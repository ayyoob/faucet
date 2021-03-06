"""Topology components for FAUCET Mininet unit tests."""

import os
import pty
import select
import socket
import string
import shutil
import subprocess
import time

import netifaces

# pylint: disable=import-error
from mininet.log import error, output
from mininet.topo import Topo
from mininet.node import Controller
from mininet.node import CPULimitedHost
from mininet.node import OVSSwitch

import mininet_test_util

# TODO: mininet 2.2.2 leaks ptys (master slave assigned in startShell)
# override as necessary close them. Transclude overridden methods
# to avoid multiple inheritance complexity.

class FaucetHostCleanup(object):
    """TODO: Mininet host implemenation leaks ptys."""

    master = None
    shell = None
    slave = None
    name = None
    inNamespace = None
    pollOut = None
    stdout = None
    execed = None
    lastCmd = None # pylint: disable=invalid-name
    readbuf = None
    lastPid = None
    pid = None
    waiting = None
    stdin = None


    def startShell(self, mnopts=None): # pylint: disable=invalid-name
        """Override Mininet startshell() to partially avoid pty leak."""
        if self.shell:
            error('%s: shell is already running\n' % self.name)
            return
        opts = '-cd' if mnopts is None else mnopts
        if self.inNamespace:
            opts += 'n'
        cmd = ['mnexec', opts, 'env', 'PS1=' + chr(127),
               'bash', '--norc', '-is', 'mininet:' + self.name]
        self.master, self.slave = pty.openpty()
        self.shell = self._popen( # pylint: disable=no-member
            cmd, stdin=self.slave, stdout=self.slave, stderr=self.slave,
            close_fds=False)
        self.stdin = os.fdopen(self.master, 'rw')
        self.stdout = self.stdin
        self.pid = self.shell.pid
        self.pollOut = select.poll() # pylint: disable=invalid-name
        self.pollOut.register(self.stdout) # pylint: disable=no-member
        self.outToNode[self.stdout.fileno()] = self # pylint: disable=no-member
        self.inToNode[self.stdin.fileno()] = self # pylint: disable=no-member
        self.execed = False
        self.lastCmd = None # pylint: disable=invalid-name
        self.lastPid = None # pylint: disable=invalid-name
        self.readbuf = ''
        while True:
            data = self.read(1024) # pylint: disable=no-member
            if data[-1] == chr(127):
                break
            self.pollOut.poll()
        self.waiting = False
        self.cmd('unset HISTFILE; stty -echo; set +m') # pylint: disable=no-member

    def terminate(self):
        """Override Mininet terminate() to partially avoid pty leak."""
        if self.shell is not None:
            os.close(self.master)
            os.close(self.slave)
            self.shell.kill()
        self.cleanup() # pylint: disable=no-member


class FaucetHost(FaucetHostCleanup, CPULimitedHost):
    """Base Mininet Host class, for Mininet-based tests."""

    pass


class FaucetSwitch(FaucetHostCleanup, OVSSwitch):
    """Switch that will be used by all tests (netdev based OVS)."""

    controller_params = {
        'controller_burst_limit': 25,
        'controller_rate_limit': 100,
    }

    def __init__(self, name, **params):
        super(FaucetSwitch, self).__init__(
            name=name, reconnectms=8000, **params)

    def start(self, controllers):
        # Transcluded from Mininet source, since need to insert
        # controller parameters at switch creation time.
        int(self.dpid, 16)  # DPID must be a hex string
        switch_intfs = [intf for intf in self.intfList() if self.ports[intf] and not intf.IP()]
        # Command to add interfaces
        intfs = ' '.join(' -- add-port %s %s' % (self, intf) +
                         self.intfOpts(intf)
                         for intf in switch_intfs)
        # Command to create controller entries
        clist = [(self.name + c.name, '%s:%s:%d' %
                 (c.protocol, c.IP(), c.port))
                 for c in controllers]
        if self.listenPort:
            clist.append((self.name + '-listen',
                           'ptcp:%s' % self.listenPort))
        ccmd = '-- --id=@%s create Controller target=\\"%s\\"'
        if self.reconnectms:
            ccmd += ' max_backoff=%d' % self.reconnectms
        for param, value in list(self.controller_params.items()):
            ccmd += ' %s=%s' % (param, value)
        cargs = ' '.join(ccmd % (name, target)
                         for name, target in clist)
        # Controller ID list
        cids = ','.join('@%s' % name for name, _target in clist)
        # Try to delete any existing bridges with the same name
        if not self.isOldOVS():
            cargs += ' -- --if-exists del-br %s' % self
        # One ovs-vsctl command to rule them all!
        self.vsctl(cargs +
                   ' -- add-br %s' % self +
                   ' -- set bridge %s controller=[%s]' % (self, cids) +
                   self.bridgeOpts() +
                   intfs )
        # switch interfaces on mininet host, must have no IP config.
        for intf in switch_intfs:
            for ipv in (4, 6):
                self.cmd('ip -%u addr flush dev %s' % (ipv, intf))
            assert '' == self.cmd('echo 1 > /proc/sys/net/ipv6/conf/%s/disable_ipv6' % intf)
        # If necessary, restore TC config overwritten by OVS
        if not self.batch:
            for intf in self.intfList():
                self.TCReapply(intf)


class VLANHost(FaucetHost):
    """Implementation of a Mininet host on a tagged VLAN."""

    intf_root_name = None

    def config(self, vlan=100, **params):
        """Configure VLANHost according to (optional) parameters:
           vlan: VLAN ID for default interface"""
        super_config = super(VLANHost, self).config(**params)
        intf = self.defaultIntf()
        vlan_intf_name = '%s.%d' % (intf, vlan)
        for cmd in (
                'ip -4 addr flush dev %s' % intf,
                'ip -6 addr flush dev %s' % intf,
                'vconfig add %s %d' % (intf, vlan),
                'ip link set dev %s up' % vlan_intf_name,
                'ip -4 addr add %s dev %s' % (params['ip'], vlan_intf_name)):
            self.cmd(cmd)
        self.intf_root_name = intf.name
        intf.name = vlan_intf_name
        self.nameToIntf[vlan_intf_name] = intf
        return super_config


class FaucetSwitchTopo(Topo):
    """FAUCET switch topology that contains a software switch."""

    CPUF = 0.5
    DELAY = '1ms'

    @staticmethod
    def _get_sid_prefix(ports_served):
        """Return a unique switch/host prefix for a test."""
        # Linux tools require short interface names.
        # pylint: disable=no-member
        id_chars = string.letters + string.digits
        id_a = int(ports_served / len(id_chars))
        id_b = ports_served - (id_a * len(id_chars))
        return '%s%s' % (
            id_chars[id_a], id_chars[id_b])

    def _add_tagged_host(self, sid_prefix, tagged_vid, host_n):
        """Add a single tagged test host."""
        host_name = 't%s%1.1u' % (sid_prefix, host_n + 1)
        return self.addHost(
            name=host_name, cls=VLANHost, vlan=tagged_vid, cpu=self.CPUF)

    def _add_untagged_host(self, sid_prefix, host_n):
        """Add a single untagged test host."""
        host_name = 'u%s%1.1u' % (sid_prefix, host_n + 1)
        return self.addHost(name=host_name, cls=FaucetHost, cpu=self.CPUF)

    def _add_faucet_switch(self, sid_prefix, dpid, ovs_type):
        """Add a FAUCET switch."""
        switch_name = 's%s' % sid_prefix
        return self.addSwitch(
            name=switch_name,
            cls=FaucetSwitch,
            datapath=ovs_type,
            dpid=mininet_test_util.mininet_dpid(dpid))

    def _add_links(self, switch, hosts, links_per_host):
        for host in hosts:
            for _ in range(links_per_host):
                self.addLink(host, switch, delay=self.DELAY, use_htb=True)

    def build(self, ovs_type, ports_sock, test_name, dpids,
              n_tagged=0, tagged_vid=100, n_untagged=0, links_per_host=0):
        for dpid in dpids:
            serialno = mininet_test_util.get_serialno(
                ports_sock, test_name)
            sid_prefix = self._get_sid_prefix(serialno)
            for host_n in range(n_tagged):
                self._add_tagged_host(sid_prefix, tagged_vid, host_n)
            for host_n in range(n_untagged):
                self._add_untagged_host(sid_prefix, host_n)
            switch = self._add_faucet_switch(sid_prefix, dpid, ovs_type)
            self._add_links(switch, self.hosts(), links_per_host)


class FaucetHwSwitchTopo(FaucetSwitchTopo):
    """FAUCET switch topology that contains a hardware switch."""

    def build(self, ovs_type, ports_sock, test_name, dpids,
              n_tagged=0, tagged_vid=100, n_untagged=0, links_per_host=0):
        for dpid in dpids:
            serialno = mininet_test_util.get_serialno(
                ports_sock, test_name)
            sid_prefix = self._get_sid_prefix(serialno)
            for host_n in range(n_tagged):
                self._add_tagged_host(sid_prefix, tagged_vid, host_n)
            for host_n in range(n_untagged):
                self._add_untagged_host(sid_prefix, host_n)
            remap_dpid = str(int(dpid) + 1)
            output('bridging hardware switch DPID %s (%x) dataplane via OVS DPID %s (%x)' % (
                dpid, int(dpid), remap_dpid, int(remap_dpid)))
            dpid = remap_dpid
            switch = self._add_faucet_switch(sid_prefix, dpid, ovs_type)
            self._add_links(switch, self.hosts(), links_per_host)


class FaucetStringOfDPSwitchTopo(FaucetSwitchTopo):
    """String of datapaths each with hosts with a single FAUCET controller."""

    switch_to_switch_links = 1

    def build(self, ovs_type, ports_sock, test_name, dpids,
              n_tagged=0, tagged_vid=100, n_untagged=0,
              links_per_host=0, switch_to_switch_links=1):
        """

                               Hosts
                               ||||
                               ||||
                 +----+       +----+       +----+
              ---+1   |       |1234|       |   1+---
        Hosts ---+2   |       |    |       |   2+--- Hosts
              ---+3   |       |    |       |   3+---
              ---+4  5+-------+5  6+-------+5  4+---
                 +----+       +----+       +----+

                 Faucet-1     Faucet-2     Faucet-3

                   |            |            |
                   |            |            |
                   +-------- controller -----+

        * s switches (above S = 3; for S > 3, switches are added to the chain)
        * (n_tagged + n_untagged) hosts per switch
        * (n_tagged + n_untagged + 1) links on switches 0 and s-1,
          with final link being inter-switch
        * (n_tagged + n_untagged + 2) links on switches 0 < n < s-1,
          with final two links being inter-switch
        """
        last_switch = None
        self.switch_to_switch_links = switch_to_switch_links
        for dpid in dpids:
            serialno = mininet_test_util.get_serialno(
                ports_sock, test_name)
            sid_prefix = self._get_sid_prefix(serialno)
            hosts = []
            for host_n in range(n_tagged):
                hosts.append(self._add_tagged_host(sid_prefix, tagged_vid, host_n))
            for host_n in range(n_untagged):
                hosts.append(self._add_untagged_host(sid_prefix, host_n))
            switch = self._add_faucet_switch(sid_prefix, dpid, ovs_type)
            self._add_links(switch, hosts, links_per_host)
            if last_switch is not None:
                # Add a switch-to-switch link with the previous switch,
                # if this isn't the first switch in the topology.
                for _ in range(self.switch_to_switch_links):
                    self.addLink(last_switch, switch)
            last_switch = switch


class BaseFAUCET(Controller):
    """Base class for FAUCET and Gauge controllers."""

    # Set to True to have cProfile output to controller log.
    CPROFILE = False
    controller_intf = None
    controller_ip = None
    pid_file = None
    tmpdir = None
    ofcap = None
    MAX_OF_PKTS = 5000
    MAX_CTL_TIME = 300

    BASE_CARGS = ' '.join((
        '--verbose',
        '--use-stderr',
        '--ryu-ofp-tcp-listen-port=%s'))

    RYU_CONF = """
[DEFAULT]
echo_request_interval=10
maximum_unreplied_echo_requests=5
socket_timeout=15
"""

    def __init__(self, name, tmpdir, controller_intf=None, cargs='', **kwargs):
        name = '%s-%u' % (name, os.getpid())
        self.tmpdir = tmpdir
        self.controller_intf = controller_intf
        super(BaseFAUCET, self).__init__(
            name, cargs=self._add_cargs(cargs, name), **kwargs)

    def _add_cargs(self, cargs, name):
        ofp_listen_host_arg = ''
        if self.controller_intf is not None:
            # pylint: disable=no-member
            self.controller_ip = netifaces.ifaddresses(
                self.controller_intf)[socket.AF_INET][0]['addr']
            ofp_listen_host_arg = '--ryu-ofp-listen-host=%s' % self.controller_ip
        self.pid_file = os.path.join(self.tmpdir, name + '.pid')
        pid_file_arg = '--ryu-pid-file=%s' % self.pid_file
        ryu_conf_file = os.path.join(self.tmpdir, 'ryu.conf')
        with open(ryu_conf_file, 'w') as ryu_conf:
            ryu_conf.write(self.RYU_CONF)
        ryu_conf_arg = '--ryu-config-file=%s' % ryu_conf_file
        return ' '.join((
            self.BASE_CARGS, pid_file_arg, ryu_conf_arg, ofp_listen_host_arg, cargs))

    def _start_tcpdump(self):
        """Start a tcpdump for OF port."""
        self.ofcap = os.path.join(self.tmpdir, '-'.join((self.name, 'of.cap')))
        tcpdump_args = ' '.join((
            '-s 0',
            '-e',
            '-n',
            '-U',
            '-q',
            '-W 1', # max files 1
            '-G %u' % (self.MAX_CTL_TIME - 1),
            '-c %u' % (self.MAX_OF_PKTS),
            '-i %s' % self.controller_intf,
            '-w %s' % self.ofcap,
            'tcp and port %u' % self.port,
            '>/dev/null',
            '2>/dev/null',
        ))
        self.cmd('timeout %s tcpdump %s &' % (
            self.MAX_CTL_TIME, tcpdump_args))
        for _ in range(5):
            if os.path.exists(self.ofcap):
                return
            time.sleep(1)
        assert False, 'tcpdump of OF channel did not start'

    @staticmethod
    def _tls_cargs(ofctl_port, ctl_privkey, ctl_cert, ca_certs):
        """Add TLS/cert parameters to Ryu."""
        tls_cargs = []
        for carg_val, carg_key in ((ctl_privkey, 'ryu-ctl-privkey'),
                                   (ctl_cert, 'ryu-ctl-cert'),
                                   (ca_certs, 'ryu-ca-certs')):
            if carg_val:
                tls_cargs.append(('--%s=%s' % (carg_key, carg_val)))
        if tls_cargs:
            tls_cargs.append(('--ryu-ofp-ssl-listen-port=%u' % ofctl_port))
        return ' '.join(tls_cargs)

    def _command(self, env, tmpdir, name, args):
        """Wrap controller startup command in shell script with environment."""
        env_vars = []
        for var, val in list(sorted(env.items())):
            env_vars.append('='.join((var, val)))
        script_wrapper_name = os.path.join(tmpdir, 'start-%s.sh' % name)
        cprofile_args = ''
        if self.CPROFILE:
            cprofile_args = 'python3 -m cProfile -s time'
        full_faucet_dir = os.path.abspath(mininet_test_util.FAUCET_DIR)
        with open(script_wrapper_name, 'w') as script_wrapper:
            faucet_cli = (
                'PYTHONPATH=%s %s exec timeout %u %s %s %s $*\n' % (
                    os.path.dirname(full_faucet_dir),
                    ' '.join(env_vars),
                    self.MAX_CTL_TIME,
                    os.path.join(full_faucet_dir, '__main__.py'),
                    cprofile_args,
                    args))
            script_wrapper.write(faucet_cli)
        return '/bin/sh %s' % script_wrapper_name

    def ryu_pid(self):
        """Return PID of ryu-manager process."""
        if os.path.exists(self.pid_file) and os.path.getsize(self.pid_file) > 0:
            pid = None
            with open(self.pid_file) as pid_file:
                pid = int(pid_file.read())
            return pid
        return None

    def listen_port(self, port, state='LISTEN'):
        """Return True if port in specified TCP state."""
        for ipv in (4, 6):
            listening_out = self.cmd(
                mininet_test_util.tcp_listening_cmd(port, ipv=ipv, state=state)).split()
            for pid in listening_out:
                if int(pid) == self.ryu_pid():
                    return True
        return False

    # pylint: disable=invalid-name
    @staticmethod
    def checkListening():
        """Mininet's checkListening() causes occasional false positives (with
           exceptions we can't catch), and we handle port conflicts ourselves anyway."""
        return

    def listening(self):
        """Return True if controller listening on required ports."""
        return self.listen_port(self.port)

    def connected(self):
        """Return True if at least one switch connected and controller healthy."""
        return self.healthy() and self.listen_port(self.port, state='ESTABLISHED')

    def logname(self):
        """Return log file for controller."""
        return os.path.join('/tmp', self.name + '.log')

    def healthy(self):
        """Return True if controller logging and listening on required ports."""
        if (os.path.exists(self.logname()) and
                os.path.getsize(self.logname()) and
                self.listening()):
            return True
        return False

    def start(self):
        """Start tcpdump for OF port and then start controller."""
        self._start_tcpdump()
        super(BaseFAUCET, self).start()

    def _stop_cap(self):
        """Stop tcpdump for OF port and run tshark to decode it."""
        if os.path.exists(self.ofcap):
            self.cmd(' '.join(['fuser', '-15', '-m', self.ofcap]))
            text_ofcap_log = '%s.txt' % self.ofcap
            with open(text_ofcap_log, 'w') as text_ofcap:
                subprocess.call(
                    ['timeout', str(self.MAX_CTL_TIME),
                     'tshark', '-l', '-n', '-Q',
                     '-d', 'tcp.port==%u,openflow' % self.port,
                     '-O', 'openflow_v4',
                     '-Y', 'openflow_v4',
                     '-r', self.ofcap],
                    stdout=text_ofcap,
                    stdin=mininet_test_util.DEVNULL,
                    stderr=mininet_test_util.DEVNULL,
                    close_fds=True)

    def stop(self):
        """Stop controller."""
        while self.healthy():
            if self.CPROFILE:
                os.kill(self.ryu_pid(), 2)
            else:
                os.kill(self.ryu_pid(), 15)
            time.sleep(1)
        self._stop_cap()
        super(BaseFAUCET, self).stop()
        if os.path.exists(self.logname()):
            tmpdir_logname = os.path.join(
                self.tmpdir, os.path.basename(self.logname()))
            if os.path.exists(tmpdir_logname):
                os.remove(tmpdir_logname)
            shutil.move(self.logname(), tmpdir_logname)


class FAUCET(BaseFAUCET):
    """Start a FAUCET controller."""

    START_ARGS = ['--ryu-app=ryu.app.ofctl_rest']

    def __init__(self, name, tmpdir, controller_intf, env,
                 ctl_privkey, ctl_cert, ca_certs,
                 ports_sock, prom_port, port, test_name, **kwargs):
        self.prom_port = prom_port
        self.ofctl_port = mininet_test_util.find_free_port(
            ports_sock, test_name)
        cargs = ' '.join((
            '--ryu-wsapi-host=%s' % mininet_test_util.LOCALHOST,
            '--ryu-wsapi-port=%u' % self.ofctl_port,
            self._tls_cargs(port, ctl_privkey, ctl_cert, ca_certs)))
        super(FAUCET, self).__init__(
            name,
            tmpdir,
            controller_intf,
            cargs=cargs,
            command=self._command(env, tmpdir, name, ' '.join(self.START_ARGS)),
            port=port,
            **kwargs)

    def listening(self):
        return (
            self.listen_port(self.ofctl_port) and
            self.listen_port(self.prom_port) and
            super(FAUCET, self).listening())


class Gauge(BaseFAUCET):
    """Start a Gauge controller."""

    def __init__(self, name, tmpdir, controller_intf, env,
                 ctl_privkey, ctl_cert, ca_certs,
                 port, **kwargs):
        super(Gauge, self).__init__(
            name,
            tmpdir,
            controller_intf,
            cargs=self._tls_cargs(port, ctl_privkey, ctl_cert, ca_certs),
            command=self._command(env, tmpdir, name, '--gauge'),
            port=port,
            **kwargs)


class FaucetExperimentalAPI(FAUCET):
    """Start a controller to run the Faucet experimental API tests."""

    START_ARGS = ['--ryu-app=test_experimental_api.py', '--ryu-app=ryu.app.ofctl_rest']
