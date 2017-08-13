#!/usr/bin/python
#
# Copyright 2014-2017 Cumulus Networks, Inc. All rights reserved.
# Author: Roopa Prabhu, roopa@cumulusnetworks.com
#

try:
    from sets import Set
    from ipaddr import IPv4Address

    from ifupdown.iface import *
    from ifupdown.utils import utils
    from ifupdown.netlink import netlink

    import ifupdown.policymanager as policymanager

    from ifupdownaddons.cache import *
    from ifupdownaddons.iproute2 import iproute2
    from ifupdownaddons.modulebase import moduleBase
    from ifupdownaddons.systemutils import systemUtils

    from nlmanager.nlmanager import Link
except ImportError, e:
    raise ImportError('%s - required module not found' % str(e))


class vxlan(moduleBase):
    _modinfo = {'mhelp' : 'vxlan module configures vxlan interfaces.',
                'attrs' : {
                        'vxlan-id' :
                            {'help' : 'vxlan id',
                             'validrange' : ['1', '16777214'],
                             'required' : True,
                             'example': ['vxlan-id 100']},
                        'vxlan-local-tunnelip' :
                            {'help' : 'vxlan local tunnel ip',
                             'validvals' : ['<ipv4>'],
                             'example': ['vxlan-local-tunnelip 172.16.20.103']},
                        'vxlan-svcnodeip' :
                            {'help' : 'vxlan id',
                             'validvals' : ['<ipv4>'],
                             'example': ['vxlan-svcnodeip 172.16.22.125']},
                        'vxlan-remoteip' :
                            {'help' : 'vxlan remote ip',
                             'validvals' : ['<ipv4>'],
                             'example': ['vxlan-remoteip 172.16.22.127'],
                             'multiline': True},
                        'vxlan-learning' :
                            {'help' : 'vxlan learning yes/no',
                             'validvals' : ['yes', 'no', 'on', 'off'],
                             'example': ['vxlan-learning no'],
                             'default': 'yes'},
                        'vxlan-ageing' :
                            {'help' : 'vxlan aging timer',
                             'validrange' : ['0', '4096'],
                             'example': ['vxlan-ageing 300'],
                             'default': '300'},
                        'vxlan-purge-remotes' :
                            {'help' : 'vxlan purge existing remote entries',
                             'validvals' : ['yes', 'no'],
                             'example': ['vxlan-purge-remotes yes']}
                }}
    _clagd_vxlan_anycast_ip = ""

    def __init__(self, *args, **kargs):
        moduleBase.__init__(self, *args, **kargs)
        self.ipcmd = None
        purge_remotes = policymanager.policymanager_api.get_module_globals(module_name=self.__class__.__name__, attr='vxlan-purge-remotes')
        if purge_remotes:
            self._purge_remotes = utils.get_boolean_from_string(purge_remotes)
        else:
            self._purge_remotes = False

    def get_dependent_ifacenames(self, ifaceobj, ifaceobjs_all=None):
        if self._is_vxlan_device(ifaceobj):
            ifaceobj.link_kind |= ifaceLinkKind.VXLAN
        elif ifaceobj.name == 'lo':
            clagd_vxlan_list = ifaceobj.get_attr_value('clagd-vxlan-anycast-ip')
            if clagd_vxlan_list:
                if len(clagd_vxlan_list) != 1:
                    self.log_warn('%s: multiple clagd-vxlan-anycast-ip lines, using first one'
                                  % (ifaceobj.name,))
                vxlan._clagd_vxlan_anycast_ip = clagd_vxlan_list[0]
        return None

    def _is_vxlan_device(self, ifaceobj):
        if ifaceobj.get_attr_value_first('vxlan-id'):
            return True
        return False

    def _get_purge_remotes(self, ifaceobj):
        if not ifaceobj:
            return self._purge_remotes
        purge_remotes = ifaceobj.get_attr_value_first('vxlan-purge-remotes')
        if purge_remotes:
            purge_remotes = utils.get_boolean_from_string(purge_remotes)
        else:
            purge_remotes = self._purge_remotes
        return purge_remotes

    def should_create_set_vxlan(self, link_exists, ifname, vxlan_id, local, learning, group):
        """
            should we issue a netlink: ip link add dev %ifname type vxlan ...?
            checking each attribute against the cache
        """
        if not link_exists:
            return True
        for attr_list, value in (
            ((ifname, 'linkinfo', Link.IFLA_VXLAN_ID), vxlan_id),
            ((ifname, 'linkinfo', 'local'), local),
            ((ifname, 'linkinfo', Link.IFLA_VXLAN_LEARNING), learning),
            ((ifname, 'linkinfo', 'svcnode'), group)
        ):
            if not self.ipcmd.cache_check(attr_list, value):
                return True
        return False

    def _vxlan_create(self, ifaceobj):
        vxlanid = ifaceobj.get_attr_value_first('vxlan-id')
        if vxlanid:
            ifname = ifaceobj.name
            anycastip = self._clagd_vxlan_anycast_ip
            group = ifaceobj.get_attr_value_first('vxlan-svcnodeip')
            local = ifaceobj.get_attr_value_first('vxlan-local-tunnelip')
            ageing = ifaceobj.get_attr_value_first('vxlan-ageing')
            purge_remotes = self._get_purge_remotes(ifaceobj)

            link_exists = self.ipcmd.link_exists(ifname)

            if (not link_exists or
                not ifaceobj.link_privflags & ifaceLinkPrivFlags.BRIDGE_PORT):
                vxlan_learning = ifaceobj.get_attr_value_first('vxlan-learning')
                if not vxlan_learning:
                    vxlan_learning = self.get_attr_default_value('vxlan-learning')
                learning = utils.get_boolean_from_string(vxlan_learning)
            else:
                learning = utils.get_boolean_from_string(
                                self.ipcmd.get_vxlandev_learning(ifname))
            
            if link_exists:
                vxlanattrs = self.ipcmd.get_vxlandev_attrs(ifname)
                # on ifreload do not overwrite anycast_ip to individual ip
                # if clagd has modified
                if vxlanattrs:
                    running_localtunnelip = vxlanattrs.get('local')
                    if (anycastip and running_localtunnelip and
                                anycastip == running_localtunnelip):
                        local = running_localtunnelip
                    if vxlanattrs.get('vxlanid') != vxlanid:
                        self.log_error('%s: Cannot change running vxlan id: '
                                       'Operation not supported' % ifname, ifaceobj)

            vxlanid = int(vxlanid)
            if self.should_create_set_vxlan(link_exists, ifname, vxlanid, local, learning, group):
                netlink.link_add_vxlan(ifname, vxlanid,
                                       local=local,
                                       learning=learning,
                                       ageing=ageing,
                                       group=group)
                # manually adding an entry to the caching after creating/updating the vxlan
                if not ifname in linkCache.links:
                    linkCache.links[ifname] = {'linkinfo': {}}
                linkCache.links[ifname]['linkinfo'].update({
                    'learning': learning,
                    Link.IFLA_VXLAN_LEARNING: learning,
                    'vxlanid': vxlanid,
                    Link.IFLA_VXLAN_ID: vxlanid,
                    'ageing': ageing,
                    Link.IFLA_VXLAN_AGEING: int(ageing),
                })
            else:
                self.logger.info('%s: vxlan already exists' % ifname)
                # if the vxlan already exists it's already cached

            remoteips = ifaceobj.get_attr_value('vxlan-remoteip')
            if remoteips:
                try:
                    for remoteip in remoteips:
                        IPv4Address(remoteip)
                except Exception as e:
                    self.log_error('%s: vxlan-remoteip: %s' %(ifaceobj.name, str(e)))

            if purge_remotes or remoteips:
                # figure out the diff for remotes and do the bridge fdb updates
                # only if provisioned by user and not by an vxlan external
                # controller.
                peers = self.ipcmd.get_vxlan_peers(ifaceobj.name, group)
                if local and remoteips and local in remoteips:
                    remoteips.remove(local)
                cur_peers = set(peers)
                if remoteips:
                    new_peers = set(remoteips)
                    del_list = cur_peers.difference(new_peers)
                    add_list = new_peers.difference(cur_peers)
                else:
                    del_list = cur_peers
                    add_list = []

                for addr in del_list:
                    try:
                        self.ipcmd.bridge_fdb_del(ifaceobj.name,
                                                  '00:00:00:00:00:00',
                                                  None, True, addr)
                    except:
                        pass

                for addr in add_list:
                    try:
                        self.ipcmd.bridge_fdb_append(ifaceobj.name,
                                                     '00:00:00:00:00:00',
                                                     None, True, addr)
                    except:
                        pass

    def _up(self, ifaceobj):
        self._vxlan_create(ifaceobj)

    def _down(self, ifaceobj):
        try:
            self.ipcmd.link_delete(ifaceobj.name)
        except Exception, e:
            self.log_warn(str(e))

    def _query_check_n_update(self, ifaceobj, ifaceobjcurr, attrname, attrval,
                              running_attrval):
        if not ifaceobj.get_attr_value_first(attrname):
            return
        if running_attrval and attrval == running_attrval:
           ifaceobjcurr.update_config_with_status(attrname, attrval, 0)
        else:
           ifaceobjcurr.update_config_with_status(attrname, running_attrval, 1)

    def _query_check_n_update_addresses(self, ifaceobjcurr, attrname,
                                        addresses, running_addresses):
        if addresses:
            for a in addresses: 
                if a in running_addresses:
                    ifaceobjcurr.update_config_with_status(attrname, a, 0)
                else:
                    ifaceobjcurr.update_config_with_status(attrname, a, 1)
            running_addresses = Set(running_addresses).difference(
                                                    Set(addresses))
        [ifaceobjcurr.update_config_with_status(attrname, a, 1)
                    for a in running_addresses]

    def _query_check(self, ifaceobj, ifaceobjcurr):
        if not self.ipcmd.link_exists(ifaceobj.name):
           return
        # Update vxlan object
        vxlanattrs = self.ipcmd.get_vxlandev_attrs(ifaceobj.name)
        if not vxlanattrs:
            ifaceobjcurr.check_n_update_config_with_status_many(ifaceobj,
                    self.get_mod_attrs(), -1)
            return
        self._query_check_n_update(ifaceobj, ifaceobjcurr, 'vxlan-id',
                       ifaceobj.get_attr_value_first('vxlan-id'), 
                       vxlanattrs.get('vxlanid'))

        running_attrval = vxlanattrs.get('local')
        attrval = ifaceobj.get_attr_value_first('vxlan-local-tunnelip')
        if running_attrval == self._clagd_vxlan_anycast_ip:
            # if local ip is anycast_ip, then let query_check to go through
            attrval = self._clagd_vxlan_anycast_ip
        self._query_check_n_update(ifaceobj, ifaceobjcurr, 'vxlan-local-tunnelip',
                                   attrval, running_attrval)

        self._query_check_n_update(ifaceobj, ifaceobjcurr, 'vxlan-svcnodeip',
                       ifaceobj.get_attr_value_first('vxlan-svcnodeip'),
                       vxlanattrs.get('svcnode'))

        purge_remotes = self._get_purge_remotes(ifaceobj)
        if purge_remotes or ifaceobj.get_attr_value('vxlan-remoteip'):
            # If purge remotes or if vxlan-remoteip's are set
            # in the config file, we are owners of the installed
            # remote-ip's, lets check and report any remote ips we don't
            # understand
            self._query_check_n_update_addresses(ifaceobjcurr, 'vxlan-remoteip',
                           ifaceobj.get_attr_value('vxlan-remoteip'),
                                                 self.ipcmd.get_vxlan_peers(ifaceobj.name, vxlanattrs.get('svcnode')))

        learning = ifaceobj.get_attr_value_first('vxlan-learning')
        if learning:
            running_learning = vxlanattrs.get('learning')
            if learning == 'yes' and running_learning == 'on':
                running_learning = 'yes'
            elif learning == 'no' and running_learning == 'off':
                running_learning = 'no'
            if learning == running_learning:
                ifaceobjcurr.update_config_with_status('vxlan-learning',
                                                        running_learning, 0)
            else:
                ifaceobjcurr.update_config_with_status('vxlan-learning',
                                                        running_learning, 1)
        ageing = ifaceobj.get_attr_value_first('vxlan-ageing')
        if not ageing:
            ageing = self.get_mod_subattr('vxlan-ageing', 'default')
        self._query_check_n_update(ifaceobj, ifaceobjcurr, 'vxlan-ageing',
                       ageing, vxlanattrs.get('ageing'))

    def _query_running(self, ifaceobjrunning):
        vxlanattrs = self.ipcmd.get_vxlandev_attrs(ifaceobjrunning.name)
        if not vxlanattrs:
            return
        attrval = vxlanattrs.get('vxlanid')
        if attrval:
            ifaceobjrunning.update_config('vxlan-id', vxlanattrs.get('vxlanid'))
        else:
            # if there is no vxlan id, this is not a vxlan port
            return
        attrval = vxlanattrs.get('local')
        if attrval:
            ifaceobjrunning.update_config('vxlan-local-tunnelip', attrval)
        attrval = vxlanattrs.get('svcnode')
        if attrval:
            ifaceobjrunning.update_config('vxlan-svcnode', attrval)
        purge_remotes = self._get_purge_remotes(None)
        if purge_remotes:
            # if purge_remotes is on, it means we own the
            # remote ips. Query them and add it to the running config
            attrval = self.ipcmd.get_vxlan_peers(ifaceobjrunning.name, vxlanattrs.get('svcnode'))
            if attrval:
                [ifaceobjrunning.update_config('vxlan-remoteip', a)
                            for a in attrval]
        attrval = vxlanattrs.get('learning')
        if attrval and attrval == 'on':
            ifaceobjrunning.update_config('vxlan-learning', 'on')
        attrval = vxlanattrs.get('ageing')
        if attrval:
            ifaceobjrunning.update_config('vxlan-ageing', vxlanattrs.get('ageing'))

    _run_ops = {'pre-up' : _up,
               'post-down' : _down,
               'query-checkcurr' : _query_check,
               'query-running' : _query_running}

    def get_ops(self):
        return self._run_ops.keys()

    def _init_command_handlers(self):
        if not self.ipcmd:
            self.ipcmd = iproute2()

    def run(self, ifaceobj, operation, query_ifaceobj=None, **extra_args):
        op_handler = self._run_ops.get(operation)
        if not op_handler:
            return
        if (operation != 'query-running' and
                not self._is_vxlan_device(ifaceobj)):
            return
        self._init_command_handlers()
        if operation == 'query-checkcurr':
            op_handler(self, ifaceobj, query_ifaceobj)
        else:
            op_handler(self, ifaceobj)
