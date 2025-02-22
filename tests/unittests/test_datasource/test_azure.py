# This file is part of cloud-init. See LICENSE file for license information.

from cloudinit import distros
from cloudinit import helpers
from cloudinit import url_helper
from cloudinit.sources import (
    UNSET, DataSourceAzure as dsaz, InvalidMetaDataException)
from cloudinit.util import (b64e, decode_binary, load_file, write_file,
                            MountFailedError, json_dumps, load_json)
from cloudinit.version import version_string as vs
from cloudinit.tests.helpers import (
    HttprettyTestCase, CiTestCase, populate_dir, mock, wrap_and_call,
    ExitStack, resourceLocation)

import crypt
import httpretty
import json
import os
import requests
import stat
import xml.etree.ElementTree as ET
import yaml


def construct_valid_ovf_env(data=None, pubkeys=None,
                            userdata=None, platform_settings=None):
    if data is None:
        data = {'HostName': 'FOOHOST'}
    if pubkeys is None:
        pubkeys = {}

    content = """<?xml version="1.0" encoding="utf-8"?>
<Environment xmlns="http://schemas.dmtf.org/ovf/environment/1"
 xmlns:oe="http://schemas.dmtf.org/ovf/environment/1"
 xmlns:wa="http://schemas.microsoft.com/windowsazure"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">

 <wa:ProvisioningSection><wa:Version>1.0</wa:Version>
 <LinuxProvisioningConfigurationSet
  xmlns="http://schemas.microsoft.com/windowsazure"
  xmlns:i="http://www.w3.org/2001/XMLSchema-instance">
  <ConfigurationSetType>LinuxProvisioningConfiguration</ConfigurationSetType>
    """
    for key, dval in data.items():
        if isinstance(dval, dict):
            val = dict(dval).get('text')
            attrs = ' ' + ' '.join(["%s='%s'" % (k, v) for k, v
                                    in dict(dval).items() if k != 'text'])
        else:
            val = dval
            attrs = ""
        content += "<%s%s>%s</%s>\n" % (key, attrs, val, key)

    if userdata:
        content += "<UserData>%s</UserData>\n" % (b64e(userdata))

    if pubkeys:
        content += "<SSH><PublicKeys>\n"
        for fp, path, value in pubkeys:
            content += " <PublicKey>"
            if fp and path:
                content += ("<Fingerprint>%s</Fingerprint><Path>%s</Path>" %
                            (fp, path))
            if value:
                content += "<Value>%s</Value>" % value
            content += "</PublicKey>\n"
        content += "</PublicKeys></SSH>"
    content += """
 </LinuxProvisioningConfigurationSet>
 </wa:ProvisioningSection>
 <wa:PlatformSettingsSection><wa:Version>1.0</wa:Version>
 <PlatformSettings xmlns="http://schemas.microsoft.com/windowsazure"
  xmlns:i="http://www.w3.org/2001/XMLSchema-instance">
 <KmsServerHostname>kms.core.windows.net</KmsServerHostname>
 <ProvisionGuestAgent>false</ProvisionGuestAgent>
 <GuestAgentPackageName i:nil="true" />"""
    if platform_settings:
        for k, v in platform_settings.items():
            content += "<%s>%s</%s>\n" % (k, v, k)
    content += """</PlatformSettings></wa:PlatformSettingsSection>
</Environment>"""

    return content


NETWORK_METADATA = {
    "compute": {
        "location": "eastus2",
        "name": "my-hostname",
        "offer": "UbuntuServer",
        "osType": "Linux",
        "placementGroupId": "",
        "platformFaultDomain": "0",
        "platformUpdateDomain": "0",
        "publisher": "Canonical",
        "resourceGroupName": "srugroup1",
        "sku": "19.04-DAILY",
        "subscriptionId": "12aad61c-6de4-4e53-a6c6-5aff52a83777",
        "tags": "",
        "version": "19.04.201906190",
        "vmId": "ff702a6b-cb6a-4fcd-ad68-b4ce38227642",
        "vmScaleSetName": "",
        "vmSize": "Standard_DS1_v2",
        "zone": ""
    },
    "network": {
        "interface": [
            {
                "macAddress": "000D3A047598",
                "ipv6": {
                    "ipAddress": []
                },
                "ipv4": {
                    "subnet": [
                        {
                           "prefix": "24",
                           "address": "10.0.0.0"
                        }
                    ],
                    "ipAddress": [
                        {
                           "privateIpAddress": "10.0.0.4",
                           "publicIpAddress": "104.46.124.81"
                        }
                    ]
                }
            }
        ]
    }
}

MOCKPATH = 'cloudinit.sources.DataSourceAzure.'


class TestGetMetadataFromIMDS(HttprettyTestCase):

    with_logs = True

    def setUp(self):
        super(TestGetMetadataFromIMDS, self).setUp()
        self.network_md_url = dsaz.IMDS_URL + "instance?api-version=2017-12-01"

    @mock.patch(MOCKPATH + 'readurl')
    @mock.patch(MOCKPATH + 'EphemeralDHCPv4')
    @mock.patch(MOCKPATH + 'net.is_up')
    def test_get_metadata_does_not_dhcp_if_network_is_up(
            self, m_net_is_up, m_dhcp, m_readurl):
        """Do not perform DHCP setup when nic is already up."""
        m_net_is_up.return_value = True
        m_readurl.return_value = url_helper.StringResponse(
            json.dumps(NETWORK_METADATA).encode('utf-8'))
        self.assertEqual(
            NETWORK_METADATA,
            dsaz.get_metadata_from_imds('eth9', retries=3))

        m_net_is_up.assert_called_with('eth9')
        m_dhcp.assert_not_called()
        self.assertIn(
            "Crawl of Azure Instance Metadata Service (IMDS) took",  # log_time
            self.logs.getvalue())

    @mock.patch(MOCKPATH + 'readurl')
    @mock.patch(MOCKPATH + 'EphemeralDHCPv4')
    @mock.patch(MOCKPATH + 'net.is_up')
    def test_get_metadata_performs_dhcp_when_network_is_down(
            self, m_net_is_up, m_dhcp, m_readurl):
        """Perform DHCP setup when nic is not up."""
        m_net_is_up.return_value = False
        m_readurl.return_value = url_helper.StringResponse(
            json.dumps(NETWORK_METADATA).encode('utf-8'))

        self.assertEqual(
            NETWORK_METADATA,
            dsaz.get_metadata_from_imds('eth9', retries=2))

        m_net_is_up.assert_called_with('eth9')
        m_dhcp.assert_called_with('eth9')
        self.assertIn(
            "Crawl of Azure Instance Metadata Service (IMDS) took",  # log_time
            self.logs.getvalue())

        m_readurl.assert_called_with(
            self.network_md_url, exception_cb=mock.ANY,
            headers={'Metadata': 'true'}, retries=2,
            timeout=dsaz.IMDS_TIMEOUT_IN_SECONDS)

    @mock.patch('cloudinit.url_helper.time.sleep')
    @mock.patch(MOCKPATH + 'net.is_up')
    def test_get_metadata_from_imds_empty_when_no_imds_present(
            self, m_net_is_up, m_sleep):
        """Return empty dict when IMDS network metadata is absent."""
        httpretty.register_uri(
            httpretty.GET,
            dsaz.IMDS_URL + 'instance?api-version=2017-12-01',
            body={}, status=404)

        m_net_is_up.return_value = True  # skips dhcp

        self.assertEqual({}, dsaz.get_metadata_from_imds('eth9', retries=2))

        m_net_is_up.assert_called_with('eth9')
        self.assertEqual([mock.call(1), mock.call(1)], m_sleep.call_args_list)
        self.assertIn(
            "Crawl of Azure Instance Metadata Service (IMDS) took",  # log_time
            self.logs.getvalue())

    @mock.patch('requests.Session.request')
    @mock.patch('cloudinit.url_helper.time.sleep')
    @mock.patch(MOCKPATH + 'net.is_up')
    def test_get_metadata_from_imds_retries_on_timeout(
            self, m_net_is_up, m_sleep, m_request):
        """Retry IMDS network metadata on timeout errors."""

        self.attempt = 0
        m_request.side_effect = requests.Timeout('Fake Connection Timeout')

        def retry_callback(request, uri, headers):
            self.attempt += 1
            raise requests.Timeout('Fake connection timeout')

        httpretty.register_uri(
            httpretty.GET,
            dsaz.IMDS_URL + 'instance?api-version=2017-12-01',
            body=retry_callback)

        m_net_is_up.return_value = True  # skips dhcp

        self.assertEqual({}, dsaz.get_metadata_from_imds('eth9', retries=3))

        m_net_is_up.assert_called_with('eth9')
        self.assertEqual([mock.call(1)]*3, m_sleep.call_args_list)
        self.assertIn(
            "Crawl of Azure Instance Metadata Service (IMDS) took",  # log_time
            self.logs.getvalue())


class TestAzureDataSource(CiTestCase):

    with_logs = True

    def setUp(self):
        super(TestAzureDataSource, self).setUp()
        self.tmp = self.tmp_dir()

        # patch cloud_dir, so our 'seed_dir' is guaranteed empty
        self.paths = helpers.Paths(
            {'cloud_dir': self.tmp, 'run_dir': self.tmp})
        self.waagent_d = os.path.join(self.tmp, 'var', 'lib', 'waagent')

        self.patches = ExitStack()
        self.addCleanup(self.patches.close)

        self.patches.enter_context(mock.patch.object(
            dsaz, '_get_random_seed', return_value='wild'))
        self.m_get_metadata_from_imds = self.patches.enter_context(
            mock.patch.object(
                dsaz, 'get_metadata_from_imds',
                mock.MagicMock(return_value=NETWORK_METADATA)))
        self.m_fallback_nic = self.patches.enter_context(
            mock.patch('cloudinit.sources.net.find_fallback_nic',
                       return_value='eth9'))
        self.m_remove_ubuntu_network_scripts = self.patches.enter_context(
            mock.patch.object(
                dsaz, 'maybe_remove_ubuntu_network_config_scripts',
                mock.MagicMock()))
        super(TestAzureDataSource, self).setUp()

    def apply_patches(self, patches):
        for module, name, new in patches:
            self.patches.enter_context(mock.patch.object(module, name, new))

    def _get_mockds(self):
        sysctl_out = "dev.storvsc.3.%pnpinfo: "\
                     "classid=ba6163d9-04a1-4d29-b605-72e2ffb1dc7f "\
                     "deviceid=f8b3781b-1e82-4818-a1c3-63d806ec15bb\n"
        sysctl_out += "dev.storvsc.2.%pnpinfo: "\
                      "classid=ba6163d9-04a1-4d29-b605-72e2ffb1dc7f "\
                      "deviceid=f8b3781a-1e82-4818-a1c3-63d806ec15bb\n"
        sysctl_out += "dev.storvsc.1.%pnpinfo: "\
                      "classid=32412632-86cb-44a2-9b5c-50d1417354f5 "\
                      "deviceid=00000000-0001-8899-0000-000000000000\n"
        camctl_devbus = """
scbus0 on ata0 bus 0
scbus1 on ata1 bus 0
scbus2 on blkvsc0 bus 0
scbus3 on blkvsc1 bus 0
scbus4 on storvsc2 bus 0
scbus5 on storvsc3 bus 0
scbus-1 on xpt0 bus 0
        """
        camctl_dev = """
<Msft Virtual CD/ROM 1.0>          at scbus1 target 0 lun 0 (cd0,pass0)
<Msft Virtual Disk 1.0>            at scbus2 target 0 lun 0 (da0,pass1)
<Msft Virtual Disk 1.0>            at scbus3 target 1 lun 0 (da1,pass2)
        """
        self.apply_patches([
            (dsaz, 'get_dev_storvsc_sysctl', mock.MagicMock(
                return_value=sysctl_out)),
            (dsaz, 'get_camcontrol_dev_bus', mock.MagicMock(
                return_value=camctl_devbus)),
            (dsaz, 'get_camcontrol_dev', mock.MagicMock(
                return_value=camctl_dev))
        ])
        return dsaz

    def _get_ds(self, data, agent_command=None, distro=None,
                apply_network=None):

        def dsdevs():
            return data.get('dsdevs', [])

        def _invoke_agent(cmd):
            data['agent_invoked'] = cmd

        def _wait_for_files(flist, _maxwait=None, _naplen=None):
            data['waited'] = flist
            return []

        def _pubkeys_from_crt_files(flist):
            data['pubkey_files'] = flist
            return ["pubkey_from: %s" % f for f in flist]

        if data.get('ovfcontent') is not None:
            populate_dir(os.path.join(self.paths.seed_dir, "azure"),
                         {'ovf-env.xml': data['ovfcontent']})

        dsaz.BUILTIN_DS_CONFIG['data_dir'] = self.waagent_d

        self.get_metadata_from_fabric = mock.MagicMock(return_value={
            'public-keys': [],
        })

        self.instance_id = 'test-instance-id'

        def _dmi_mocks(key):
            if key == 'system-uuid':
                return self.instance_id
            elif key == 'chassis-asset-tag':
                return '7783-7084-3265-9085-8269-3286-77'

        self.apply_patches([
            (dsaz, 'list_possible_azure_ds_devs', dsdevs),
            (dsaz, 'invoke_agent', _invoke_agent),
            (dsaz, 'pubkeys_from_crt_files', _pubkeys_from_crt_files),
            (dsaz, 'perform_hostname_bounce', mock.MagicMock()),
            (dsaz, 'get_hostname', mock.MagicMock()),
            (dsaz, 'set_hostname', mock.MagicMock()),
            (dsaz, 'get_metadata_from_fabric', self.get_metadata_from_fabric),
            (dsaz.util, 'which', lambda x: True),
            (dsaz.util, 'read_dmi_data', mock.MagicMock(
                side_effect=_dmi_mocks)),
            (dsaz.util, 'wait_for_files', mock.MagicMock(
                side_effect=_wait_for_files)),
        ])

        if distro is not None:
            distro_cls = distros.fetch(distro)
            distro = distro_cls(distro, data.get('sys_cfg', {}), self.paths)
        dsrc = dsaz.DataSourceAzure(
            data.get('sys_cfg', {}), distro=distro, paths=self.paths)
        if agent_command is not None:
            dsrc.ds_cfg['agent_command'] = agent_command
        if apply_network is not None:
            dsrc.ds_cfg['apply_network_config'] = apply_network

        return dsrc

    def _get_and_setup(self, dsrc):
        ret = dsrc.get_data()
        if ret:
            dsrc.setup(True)
        return ret

    def xml_equals(self, oxml, nxml):
        """Compare two sets of XML to make sure they are equal"""

        def create_tag_index(xml):
            et = ET.fromstring(xml)
            ret = {}
            for x in et.iter():
                ret[x.tag] = x
            return ret

        def tags_exists(x, y):
            for tag in x.keys():
                self.assertIn(tag, y)
            for tag in y.keys():
                self.assertIn(tag, x)

        def tags_equal(x, y):
            for x_val in x.values():
                y_val = y.get(x_val.tag)
                self.assertEqual(x_val.text, y_val.text)

        old_cnt = create_tag_index(oxml)
        new_cnt = create_tag_index(nxml)
        tags_exists(old_cnt, new_cnt)
        tags_equal(old_cnt, new_cnt)

    def xml_notequals(self, oxml, nxml):
        try:
            self.xml_equals(oxml, nxml)
        except AssertionError:
            return
        raise AssertionError("XML is the same")

    def test_get_resource_disk(self):
        ds = self._get_mockds()
        dev = ds.get_resource_disk_on_freebsd(1)
        self.assertEqual("da1", dev)

    @mock.patch(MOCKPATH + '_is_platform_viable')
    def test_call_is_platform_viable_seed(self, m_is_platform_viable):
        """Check seed_dir using _is_platform_viable and return False."""
        # Return a non-matching asset tag value
        m_is_platform_viable.return_value = False
        dsrc = dsaz.DataSourceAzure(
            {}, distro=None, paths=self.paths)
        self.assertFalse(dsrc.get_data())
        m_is_platform_viable.assert_called_with(dsrc.seed_dir)

    def test_basic_seed_dir(self):
        odata = {'HostName': "myhost", 'UserName': "myuser"}
        data = {'ovfcontent': construct_valid_ovf_env(data=odata),
                'sys_cfg': {}}
        dsrc = self._get_ds(data)
        ret = dsrc.get_data()
        self.assertTrue(ret)
        self.assertEqual(dsrc.userdata_raw, "")
        self.assertEqual(dsrc.metadata['local-hostname'], odata['HostName'])
        self.assertTrue(os.path.isfile(
            os.path.join(self.waagent_d, 'ovf-env.xml')))
        self.assertEqual('azure', dsrc.cloud_name)
        self.assertEqual('azure', dsrc.platform_type)
        self.assertEqual(
            'seed-dir (%s/seed/azure)' % self.tmp, dsrc.subplatform)

    def test_basic_dev_file(self):
        """When a device path is used, present that in subplatform."""
        data = {'sys_cfg': {}, 'dsdevs': ['/dev/cd0']}
        dsrc = self._get_ds(data)
        with mock.patch(MOCKPATH + 'util.mount_cb') as m_mount_cb:
            m_mount_cb.return_value = (
                {'local-hostname': 'me'}, 'ud', {'cfg': ''}, {})
            self.assertTrue(dsrc.get_data())
        self.assertEqual(dsrc.userdata_raw, 'ud')
        self.assertEqual(dsrc.metadata['local-hostname'], 'me')
        self.assertEqual('azure', dsrc.cloud_name)
        self.assertEqual('azure', dsrc.platform_type)
        self.assertEqual('config-disk (/dev/cd0)', dsrc.subplatform)

    def test_get_data_non_ubuntu_will_not_remove_network_scripts(self):
        """get_data on non-Ubuntu will not remove ubuntu net scripts."""
        odata = {'HostName': "myhost", 'UserName': "myuser"}
        data = {'ovfcontent': construct_valid_ovf_env(data=odata),
                'sys_cfg': {}}

        dsrc = self._get_ds(data, distro='debian')
        dsrc.get_data()
        self.m_remove_ubuntu_network_scripts.assert_not_called()

    def test_get_data_on_ubuntu_will_remove_network_scripts(self):
        """get_data will remove ubuntu net scripts on Ubuntu distro."""
        sys_cfg = {'datasource': {'Azure': {'apply_network_config': True}}}
        odata = {'HostName': "myhost", 'UserName': "myuser"}
        data = {'ovfcontent': construct_valid_ovf_env(data=odata),
                'sys_cfg': sys_cfg}

        dsrc = self._get_ds(data, distro='ubuntu')
        dsrc.get_data()
        self.m_remove_ubuntu_network_scripts.assert_called_once_with()

    def test_get_data_on_ubuntu_will_not_remove_network_scripts_disabled(self):
        """When apply_network_config false, do not remove scripts on Ubuntu."""
        sys_cfg = {'datasource': {'Azure': {'apply_network_config': False}}}
        odata = {'HostName': "myhost", 'UserName': "myuser"}
        data = {'ovfcontent': construct_valid_ovf_env(data=odata),
                'sys_cfg': sys_cfg}

        dsrc = self._get_ds(data, distro='ubuntu')
        dsrc.get_data()
        self.m_remove_ubuntu_network_scripts.assert_not_called()

    def test_crawl_metadata_returns_structured_data_and_caches_nothing(self):
        """Return all structured metadata and cache no class attributes."""
        yaml_cfg = "{agent_command: my_command}\n"
        odata = {'HostName': "myhost", 'UserName': "myuser",
                 'UserData': {'text': 'FOOBAR', 'encoding': 'plain'},
                 'dscfg': {'text': yaml_cfg, 'encoding': 'plain'}}
        data = {'ovfcontent': construct_valid_ovf_env(data=odata),
                'sys_cfg': {}}
        dsrc = self._get_ds(data)
        expected_cfg = {
            'PreprovisionedVm': False,
            'datasource': {'Azure': {'agent_command': 'my_command'}},
            'system_info': {'default_user': {'name': u'myuser'}}}
        expected_metadata = {
            'azure_data': {
                'configurationsettype': 'LinuxProvisioningConfiguration'},
            'imds': NETWORK_METADATA,
            'instance-id': 'test-instance-id',
            'local-hostname': u'myhost',
            'random_seed': 'wild'}

        crawled_metadata = dsrc.crawl_metadata()

        self.assertItemsEqual(
            crawled_metadata.keys(),
            ['cfg', 'files', 'metadata', 'userdata_raw'])
        self.assertEqual(crawled_metadata['cfg'], expected_cfg)
        self.assertEqual(
            list(crawled_metadata['files'].keys()), ['ovf-env.xml'])
        self.assertIn(
            b'<HostName>myhost</HostName>',
            crawled_metadata['files']['ovf-env.xml'])
        self.assertEqual(crawled_metadata['metadata'], expected_metadata)
        self.assertEqual(crawled_metadata['userdata_raw'], 'FOOBAR')
        self.assertEqual(dsrc.userdata_raw, None)
        self.assertEqual(dsrc.metadata, {})
        self.assertEqual(dsrc._metadata_imds, UNSET)
        self.assertFalse(os.path.isfile(
            os.path.join(self.waagent_d, 'ovf-env.xml')))

    def test_crawl_metadata_raises_invalid_metadata_on_error(self):
        """crawl_metadata raises an exception on invalid ovf-env.xml."""
        data = {'ovfcontent': "BOGUS", 'sys_cfg': {}}
        dsrc = self._get_ds(data)
        error_msg = ('BrokenAzureDataSource: Invalid ovf-env.xml:'
                     ' syntax error: line 1, column 0')
        with self.assertRaises(InvalidMetaDataException) as cm:
            dsrc.crawl_metadata()
        self.assertEqual(str(cm.exception), error_msg)

    @mock.patch('cloudinit.sources.DataSourceAzure.EphemeralDHCPv4')
    @mock.patch('cloudinit.sources.DataSourceAzure.util.write_file')
    @mock.patch(
        'cloudinit.sources.DataSourceAzure.DataSourceAzure._report_ready')
    @mock.patch('cloudinit.sources.DataSourceAzure.DataSourceAzure._poll_imds')
    def test_crawl_metadata_on_reprovision_reports_ready(
                            self, poll_imds_func,
                            report_ready_func,
                            m_write, m_dhcp):
        """If reprovisioning, report ready at the end"""
        ovfenv = construct_valid_ovf_env(
                            platform_settings={"PreprovisionedVm": "True"})

        data = {'ovfcontent': ovfenv,
                'sys_cfg': {}}
        dsrc = self._get_ds(data)
        poll_imds_func.return_value = ovfenv
        dsrc.crawl_metadata()
        self.assertEqual(1, report_ready_func.call_count)

    @mock.patch('cloudinit.sources.DataSourceAzure.util.write_file')
    @mock.patch('cloudinit.sources.helpers.netlink.'
                'wait_for_media_disconnect_connect')
    @mock.patch(
        'cloudinit.sources.DataSourceAzure.DataSourceAzure._report_ready')
    @mock.patch('cloudinit.net.dhcp.EphemeralIPv4Network')
    @mock.patch('cloudinit.net.dhcp.maybe_perform_dhcp_discovery')
    @mock.patch('cloudinit.sources.DataSourceAzure.readurl')
    def test_crawl_metadata_on_reprovision_reports_ready_using_lease(
                            self, m_readurl, m_dhcp,
                            m_net, report_ready_func,
                            m_media_switch, m_write):
        """If reprovisioning, report ready using the obtained lease"""
        ovfenv = construct_valid_ovf_env(
                            platform_settings={"PreprovisionedVm": "True"})

        data = {'ovfcontent': ovfenv,
                'sys_cfg': {}}
        dsrc = self._get_ds(data)

        lease = {
            'interface': 'eth9', 'fixed-address': '192.168.2.9',
            'routers': '192.168.2.1', 'subnet-mask': '255.255.255.0',
            'unknown-245': '624c3620'}
        m_dhcp.return_value = [lease]
        m_media_switch.return_value = None

        reprovision_ovfenv = construct_valid_ovf_env()
        m_readurl.return_value = url_helper.StringResponse(
            reprovision_ovfenv.encode('utf-8'))

        dsrc.crawl_metadata()
        self.assertEqual(2, report_ready_func.call_count)
        report_ready_func.assert_called_with(lease=lease)

    def test_waagent_d_has_0700_perms(self):
        # we expect /var/lib/waagent to be created 0700
        dsrc = self._get_ds({'ovfcontent': construct_valid_ovf_env()})
        ret = dsrc.get_data()
        self.assertTrue(ret)
        self.assertTrue(os.path.isdir(self.waagent_d))
        self.assertEqual(stat.S_IMODE(os.stat(self.waagent_d).st_mode), 0o700)

    def test_user_cfg_set_agent_command_plain(self):
        # set dscfg in via plaintext
        # we must have friendly-to-xml formatted plaintext in yaml_cfg
        # not all plaintext is expected to work.
        yaml_cfg = "{agent_command: my_command}\n"
        cfg = yaml.safe_load(yaml_cfg)
        odata = {'HostName': "myhost", 'UserName': "myuser",
                 'dscfg': {'text': yaml_cfg, 'encoding': 'plain'}}
        data = {'ovfcontent': construct_valid_ovf_env(data=odata)}

        dsrc = self._get_ds(data)
        ret = self._get_and_setup(dsrc)
        self.assertTrue(ret)
        self.assertEqual(data['agent_invoked'], cfg['agent_command'])

    def test_network_config_set_from_imds(self):
        """Datasource.network_config returns IMDS network data."""
        sys_cfg = {'datasource': {'Azure': {'apply_network_config': True}}}
        odata = {}
        data = {'ovfcontent': construct_valid_ovf_env(data=odata),
                'sys_cfg': sys_cfg}
        expected_network_config = {
            'ethernets': {
                'eth0': {'set-name': 'eth0',
                         'match': {'macaddress': '00:0d:3a:04:75:98'},
                         'dhcp4': True}},
            'version': 2}
        dsrc = self._get_ds(data)
        dsrc.get_data()
        self.assertEqual(expected_network_config, dsrc.network_config)

    def test_availability_zone_set_from_imds(self):
        """Datasource.availability returns IMDS platformFaultDomain."""
        sys_cfg = {'datasource': {'Azure': {'apply_network_config': True}}}
        odata = {}
        data = {'ovfcontent': construct_valid_ovf_env(data=odata),
                'sys_cfg': sys_cfg}
        dsrc = self._get_ds(data)
        dsrc.get_data()
        self.assertEqual('0', dsrc.availability_zone)

    def test_region_set_from_imds(self):
        """Datasource.region returns IMDS region location."""
        sys_cfg = {'datasource': {'Azure': {'apply_network_config': True}}}
        odata = {}
        data = {'ovfcontent': construct_valid_ovf_env(data=odata),
                'sys_cfg': sys_cfg}
        dsrc = self._get_ds(data)
        dsrc.get_data()
        self.assertEqual('eastus2', dsrc.region)

    def test_user_cfg_set_agent_command(self):
        # set dscfg in via base64 encoded yaml
        cfg = {'agent_command': "my_command"}
        odata = {'HostName': "myhost", 'UserName': "myuser",
                 'dscfg': {'text': b64e(yaml.dump(cfg)),
                           'encoding': 'base64'}}
        data = {'ovfcontent': construct_valid_ovf_env(data=odata)}

        dsrc = self._get_ds(data)
        ret = self._get_and_setup(dsrc)
        self.assertTrue(ret)
        self.assertEqual(data['agent_invoked'], cfg['agent_command'])

    def test_sys_cfg_set_agent_command(self):
        sys_cfg = {'datasource': {'Azure': {'agent_command': '_COMMAND'}}}
        data = {'ovfcontent': construct_valid_ovf_env(data={}),
                'sys_cfg': sys_cfg}

        dsrc = self._get_ds(data)
        ret = self._get_and_setup(dsrc)
        self.assertTrue(ret)
        self.assertEqual(data['agent_invoked'], '_COMMAND')

    def test_sys_cfg_set_never_destroy_ntfs(self):
        sys_cfg = {'datasource': {'Azure': {
            'never_destroy_ntfs': 'user-supplied-value'}}}
        data = {'ovfcontent': construct_valid_ovf_env(data={}),
                'sys_cfg': sys_cfg}

        dsrc = self._get_ds(data)
        ret = self._get_and_setup(dsrc)
        self.assertTrue(ret)
        self.assertEqual(dsrc.ds_cfg.get(dsaz.DS_CFG_KEY_PRESERVE_NTFS),
                         'user-supplied-value')

    def test_username_used(self):
        odata = {'HostName': "myhost", 'UserName': "myuser"}
        data = {'ovfcontent': construct_valid_ovf_env(data=odata)}

        dsrc = self._get_ds(data)
        ret = dsrc.get_data()
        self.assertTrue(ret)
        self.assertEqual(dsrc.cfg['system_info']['default_user']['name'],
                         "myuser")

    def test_password_given(self):
        odata = {'HostName': "myhost", 'UserName': "myuser",
                 'UserPassword': "mypass"}
        data = {'ovfcontent': construct_valid_ovf_env(data=odata)}

        dsrc = self._get_ds(data)
        ret = dsrc.get_data()
        self.assertTrue(ret)
        self.assertTrue('default_user' in dsrc.cfg['system_info'])
        defuser = dsrc.cfg['system_info']['default_user']

        # default user should be updated username and should not be locked.
        self.assertEqual(defuser['name'], odata['UserName'])
        self.assertFalse(defuser['lock_passwd'])
        # passwd is crypt formated string $id$salt$encrypted
        # encrypting plaintext with salt value of everything up to final '$'
        # should equal that after the '$'
        pos = defuser['passwd'].rfind("$") + 1
        self.assertEqual(defuser['passwd'],
                         crypt.crypt(odata['UserPassword'],
                                     defuser['passwd'][0:pos]))

    def test_userdata_plain(self):
        mydata = "FOOBAR"
        odata = {'UserData': {'text': mydata, 'encoding': 'plain'}}
        data = {'ovfcontent': construct_valid_ovf_env(data=odata)}

        dsrc = self._get_ds(data)
        ret = dsrc.get_data()
        self.assertTrue(ret)
        self.assertEqual(decode_binary(dsrc.userdata_raw), mydata)

    def test_userdata_found(self):
        mydata = "FOOBAR"
        odata = {'UserData': {'text': b64e(mydata), 'encoding': 'base64'}}
        data = {'ovfcontent': construct_valid_ovf_env(data=odata)}

        dsrc = self._get_ds(data)
        ret = dsrc.get_data()
        self.assertTrue(ret)
        self.assertEqual(dsrc.userdata_raw, mydata.encode('utf-8'))

    def test_no_datasource_expected(self):
        # no source should be found if no seed_dir and no devs
        data = {}
        dsrc = self._get_ds({})
        ret = dsrc.get_data()
        self.assertFalse(ret)
        self.assertFalse('agent_invoked' in data)

    def test_cfg_has_pubkeys_fingerprint(self):
        odata = {'HostName': "myhost", 'UserName': "myuser"}
        mypklist = [{'fingerprint': 'fp1', 'path': 'path1', 'value': ''}]
        pubkeys = [(x['fingerprint'], x['path'], x['value']) for x in mypklist]
        data = {'ovfcontent': construct_valid_ovf_env(data=odata,
                                                      pubkeys=pubkeys)}

        dsrc = self._get_ds(data, agent_command=['not', '__builtin__'])
        ret = self._get_and_setup(dsrc)
        self.assertTrue(ret)
        for mypk in mypklist:
            self.assertIn(mypk, dsrc.cfg['_pubkeys'])
            self.assertIn('pubkey_from', dsrc.metadata['public-keys'][-1])

    def test_cfg_has_pubkeys_value(self):
        # make sure that provided key is used over fingerprint
        odata = {'HostName': "myhost", 'UserName': "myuser"}
        mypklist = [{'fingerprint': 'fp1', 'path': 'path1', 'value': 'value1'}]
        pubkeys = [(x['fingerprint'], x['path'], x['value']) for x in mypklist]
        data = {'ovfcontent': construct_valid_ovf_env(data=odata,
                                                      pubkeys=pubkeys)}

        dsrc = self._get_ds(data, agent_command=['not', '__builtin__'])
        ret = self._get_and_setup(dsrc)
        self.assertTrue(ret)

        for mypk in mypklist:
            self.assertIn(mypk, dsrc.cfg['_pubkeys'])
            self.assertIn(mypk['value'], dsrc.metadata['public-keys'])

    def test_cfg_has_no_fingerprint_has_value(self):
        # test value is used when fingerprint not provided
        odata = {'HostName': "myhost", 'UserName': "myuser"}
        mypklist = [{'fingerprint': None, 'path': 'path1', 'value': 'value1'}]
        pubkeys = [(x['fingerprint'], x['path'], x['value']) for x in mypklist]
        data = {'ovfcontent': construct_valid_ovf_env(data=odata,
                                                      pubkeys=pubkeys)}

        dsrc = self._get_ds(data, agent_command=['not', '__builtin__'])
        ret = self._get_and_setup(dsrc)
        self.assertTrue(ret)

        for mypk in mypklist:
            self.assertIn(mypk['value'], dsrc.metadata['public-keys'])

    def test_default_ephemeral(self):
        # make sure the ephemeral device works
        odata = {}
        data = {'ovfcontent': construct_valid_ovf_env(data=odata),
                'sys_cfg': {}}

        dsrc = self._get_ds(data)
        ret = dsrc.get_data()
        self.assertTrue(ret)
        cfg = dsrc.get_config_obj()

        self.assertEqual(dsrc.device_name_to_device("ephemeral0"),
                         dsaz.RESOURCE_DISK_PATH)
        assert 'disk_setup' in cfg
        assert 'fs_setup' in cfg
        self.assertIsInstance(cfg['disk_setup'], dict)
        self.assertIsInstance(cfg['fs_setup'], list)

    def test_provide_disk_aliases(self):
        # Make sure that user can affect disk aliases
        dscfg = {'disk_aliases': {'ephemeral0': '/dev/sdc'}}
        odata = {'HostName': "myhost", 'UserName': "myuser",
                 'dscfg': {'text': b64e(yaml.dump(dscfg)),
                           'encoding': 'base64'}}
        usercfg = {'disk_setup': {'/dev/sdc': {'something': '...'},
                                  'ephemeral0': False}}
        userdata = '#cloud-config' + yaml.dump(usercfg) + "\n"

        ovfcontent = construct_valid_ovf_env(data=odata, userdata=userdata)
        data = {'ovfcontent': ovfcontent, 'sys_cfg': {}}

        dsrc = self._get_ds(data)
        ret = dsrc.get_data()
        self.assertTrue(ret)
        cfg = dsrc.get_config_obj()
        self.assertTrue(cfg)

    def test_userdata_arrives(self):
        userdata = "This is my user-data"
        xml = construct_valid_ovf_env(data={}, userdata=userdata)
        data = {'ovfcontent': xml}
        dsrc = self._get_ds(data)
        dsrc.get_data()

        self.assertEqual(userdata.encode('us-ascii'), dsrc.userdata_raw)

    def test_password_redacted_in_ovf(self):
        odata = {'HostName': "myhost", 'UserName': "myuser",
                 'UserPassword': "mypass"}
        data = {'ovfcontent': construct_valid_ovf_env(data=odata)}
        dsrc = self._get_ds(data)
        ret = dsrc.get_data()

        self.assertTrue(ret)
        ovf_env_path = os.path.join(self.waagent_d, 'ovf-env.xml')

        # The XML should not be same since the user password is redacted
        on_disk_ovf = load_file(ovf_env_path)
        self.xml_notequals(data['ovfcontent'], on_disk_ovf)

        # Make sure that the redacted password on disk is not used by CI
        self.assertNotEqual(dsrc.cfg.get('password'),
                            dsaz.DEF_PASSWD_REDACTION)

        # Make sure that the password was really encrypted
        et = ET.fromstring(on_disk_ovf)
        for elem in et.iter():
            if 'UserPassword' in elem.tag:
                self.assertEqual(dsaz.DEF_PASSWD_REDACTION, elem.text)

    def test_ovf_env_arrives_in_waagent_dir(self):
        xml = construct_valid_ovf_env(data={}, userdata="FOODATA")
        dsrc = self._get_ds({'ovfcontent': xml})
        dsrc.get_data()

        # 'data_dir' is '/var/lib/waagent' (walinux-agent's state dir)
        # we expect that the ovf-env.xml file is copied there.
        ovf_env_path = os.path.join(self.waagent_d, 'ovf-env.xml')
        self.assertTrue(os.path.exists(ovf_env_path))
        self.xml_equals(xml, load_file(ovf_env_path))

    def test_ovf_can_include_unicode(self):
        xml = construct_valid_ovf_env(data={})
        xml = u'\ufeff{0}'.format(xml)
        dsrc = self._get_ds({'ovfcontent': xml})
        dsrc.get_data()

    def test_exception_fetching_fabric_data_doesnt_propagate(self):
        """Errors communicating with fabric should warn, but return True."""
        dsrc = self._get_ds({'ovfcontent': construct_valid_ovf_env()})
        dsrc.ds_cfg['agent_command'] = '__builtin__'
        self.get_metadata_from_fabric.side_effect = Exception
        ret = self._get_and_setup(dsrc)
        self.assertTrue(ret)

    def test_fabric_data_included_in_metadata(self):
        dsrc = self._get_ds({'ovfcontent': construct_valid_ovf_env()})
        dsrc.ds_cfg['agent_command'] = '__builtin__'
        self.get_metadata_from_fabric.return_value = {'test': 'value'}
        ret = self._get_and_setup(dsrc)
        self.assertTrue(ret)
        self.assertEqual('value', dsrc.metadata['test'])

    def test_instance_id_from_dmidecode_used(self):
        ds = self._get_ds({'ovfcontent': construct_valid_ovf_env()})
        ds.get_data()
        self.assertEqual(self.instance_id, ds.metadata['instance-id'])

    def test_instance_id_from_dmidecode_used_for_builtin(self):
        ds = self._get_ds({'ovfcontent': construct_valid_ovf_env()})
        ds.ds_cfg['agent_command'] = '__builtin__'
        ds.get_data()
        self.assertEqual(self.instance_id, ds.metadata['instance-id'])

    @mock.patch(MOCKPATH + 'util.is_FreeBSD')
    @mock.patch(MOCKPATH + '_check_freebsd_cdrom')
    def test_list_possible_azure_ds_devs(self, m_check_fbsd_cdrom,
                                         m_is_FreeBSD):
        """On FreeBSD, possible devs should show /dev/cd0."""
        m_is_FreeBSD.return_value = True
        m_check_fbsd_cdrom.return_value = True
        self.assertEqual(dsaz.list_possible_azure_ds_devs(), ['/dev/cd0'])
        self.assertEqual(
            [mock.call("/dev/cd0")], m_check_fbsd_cdrom.call_args_list)

    @mock.patch('cloudinit.net.generate_fallback_config')
    def test_imds_network_config(self, mock_fallback):
        """Network config is generated from IMDS network data when present."""
        sys_cfg = {'datasource': {'Azure': {'apply_network_config': True}}}
        odata = {'HostName': "myhost", 'UserName': "myuser"}
        data = {'ovfcontent': construct_valid_ovf_env(data=odata),
                'sys_cfg': sys_cfg}

        dsrc = self._get_ds(data)
        ret = dsrc.get_data()
        self.assertTrue(ret)

        expected_cfg = {
            'ethernets': {
                'eth0': {'dhcp4': True,
                         'match': {'macaddress': '00:0d:3a:04:75:98'},
                         'set-name': 'eth0'}},
            'version': 2}

        self.assertEqual(expected_cfg, dsrc.network_config)
        mock_fallback.assert_not_called()

    @mock.patch('cloudinit.net.get_interface_mac')
    @mock.patch('cloudinit.net.get_devicelist')
    @mock.patch('cloudinit.net.device_driver')
    @mock.patch('cloudinit.net.generate_fallback_config')
    def test_imds_network_ignored_when_apply_network_config_false(
            self, mock_fallback, mock_dd, mock_devlist, mock_get_mac):
        """When apply_network_config is False, use fallback instead of IMDS."""
        sys_cfg = {'datasource': {'Azure': {'apply_network_config': False}}}
        odata = {'HostName': "myhost", 'UserName': "myuser"}
        data = {'ovfcontent': construct_valid_ovf_env(data=odata),
                'sys_cfg': sys_cfg}
        fallback_config = {
            'version': 1,
            'config': [{
                'type': 'physical', 'name': 'eth0',
                'mac_address': '00:11:22:33:44:55',
                'params': {'driver': 'hv_netsvc'},
                'subnets': [{'type': 'dhcp'}],
            }]
        }
        mock_fallback.return_value = fallback_config

        mock_devlist.return_value = ['eth0']
        mock_dd.return_value = ['hv_netsvc']
        mock_get_mac.return_value = '00:11:22:33:44:55'

        dsrc = self._get_ds(data)
        self.assertTrue(dsrc.get_data())
        self.assertEqual(dsrc.network_config, fallback_config)

    @mock.patch('cloudinit.net.get_interface_mac')
    @mock.patch('cloudinit.net.get_devicelist')
    @mock.patch('cloudinit.net.device_driver')
    @mock.patch('cloudinit.net.generate_fallback_config')
    def test_fallback_network_config(self, mock_fallback, mock_dd,
                                     mock_devlist, mock_get_mac):
        """On absent IMDS network data, generate network fallback config."""
        odata = {'HostName': "myhost", 'UserName': "myuser"}
        data = {'ovfcontent': construct_valid_ovf_env(data=odata),
                'sys_cfg': {}}

        fallback_config = {
            'version': 1,
            'config': [{
                'type': 'physical', 'name': 'eth0',
                'mac_address': '00:11:22:33:44:55',
                'params': {'driver': 'hv_netsvc'},
                'subnets': [{'type': 'dhcp'}],
            }]
        }
        mock_fallback.return_value = fallback_config

        mock_devlist.return_value = ['eth0']
        mock_dd.return_value = ['hv_netsvc']
        mock_get_mac.return_value = '00:11:22:33:44:55'

        dsrc = self._get_ds(data)
        # Represent empty response from network imds
        self.m_get_metadata_from_imds.return_value = {}
        ret = dsrc.get_data()
        self.assertTrue(ret)

        netconfig = dsrc.network_config
        self.assertEqual(netconfig, fallback_config)
        mock_fallback.assert_called_with(blacklist_drivers=['mlx4_core'],
                                         config_driver=True)

    @mock.patch('cloudinit.net.get_interface_mac')
    @mock.patch('cloudinit.net.get_devicelist')
    @mock.patch('cloudinit.net.device_driver')
    @mock.patch('cloudinit.net.generate_fallback_config')
    def test_fallback_network_config_blacklist(self, mock_fallback, mock_dd,
                                               mock_devlist, mock_get_mac):
        """On absent network metadata, blacklist mlx from fallback config."""
        odata = {'HostName': "myhost", 'UserName': "myuser"}
        data = {'ovfcontent': construct_valid_ovf_env(data=odata),
                'sys_cfg': {}}

        fallback_config = {
            'version': 1,
            'config': [{
                'type': 'physical', 'name': 'eth0',
                'mac_address': '00:11:22:33:44:55',
                'params': {'driver': 'hv_netsvc'},
                'subnets': [{'type': 'dhcp'}],
            }]
        }
        blacklist_config = {
            'type': 'physical',
            'name': 'eth1',
            'mac_address': '00:11:22:33:44:55',
            'params': {'driver': 'mlx4_core'}
        }
        mock_fallback.return_value = fallback_config

        mock_devlist.return_value = ['eth0', 'eth1']
        mock_dd.side_effect = [
            'hv_netsvc',  # list composition, skipped
            'mlx4_core',  # list composition, match
            'mlx4_core',  # config get driver name
        ]
        mock_get_mac.return_value = '00:11:22:33:44:55'

        dsrc = self._get_ds(data)
        # Represent empty response from network imds
        self.m_get_metadata_from_imds.return_value = {}
        ret = dsrc.get_data()
        self.assertTrue(ret)

        netconfig = dsrc.network_config
        expected_config = fallback_config
        expected_config['config'].append(blacklist_config)
        self.assertEqual(netconfig, expected_config)

    @mock.patch(MOCKPATH + 'util.subp')
    def test_get_hostname_with_no_args(self, subp):
        dsaz.get_hostname()
        subp.assert_called_once_with(("hostname",), capture=True)

    @mock.patch(MOCKPATH + 'util.subp')
    def test_get_hostname_with_string_arg(self, subp):
        dsaz.get_hostname(hostname_command="hostname")
        subp.assert_called_once_with(("hostname",), capture=True)

    @mock.patch(MOCKPATH + 'util.subp')
    def test_get_hostname_with_iterable_arg(self, subp):
        dsaz.get_hostname(hostname_command=("hostname",))
        subp.assert_called_once_with(("hostname",), capture=True)


class TestAzureBounce(CiTestCase):

    with_logs = True

    def mock_out_azure_moving_parts(self):
        self.patches.enter_context(
            mock.patch.object(dsaz, 'invoke_agent'))
        self.patches.enter_context(
            mock.patch.object(dsaz.util, 'wait_for_files'))
        self.patches.enter_context(
            mock.patch.object(dsaz, 'list_possible_azure_ds_devs',
                              mock.MagicMock(return_value=[])))
        self.patches.enter_context(
            mock.patch.object(dsaz, 'get_metadata_from_fabric',
                              mock.MagicMock(return_value={})))
        self.patches.enter_context(
            mock.patch.object(dsaz, 'get_metadata_from_imds',
                              mock.MagicMock(return_value={})))
        self.patches.enter_context(
            mock.patch.object(dsaz.util, 'which', lambda x: True))
        self.patches.enter_context(mock.patch.object(
            dsaz, '_get_random_seed', return_value='wild'))

        def _dmi_mocks(key):
            if key == 'system-uuid':
                return 'test-instance-id'
            elif key == 'chassis-asset-tag':
                return '7783-7084-3265-9085-8269-3286-77'
            raise RuntimeError('should not get here')

        self.patches.enter_context(
            mock.patch.object(dsaz.util, 'read_dmi_data',
                              mock.MagicMock(side_effect=_dmi_mocks)))

    def setUp(self):
        super(TestAzureBounce, self).setUp()
        self.tmp = self.tmp_dir()
        self.waagent_d = os.path.join(self.tmp, 'var', 'lib', 'waagent')
        self.paths = helpers.Paths(
            {'cloud_dir': self.tmp, 'run_dir': self.tmp})
        dsaz.BUILTIN_DS_CONFIG['data_dir'] = self.waagent_d
        self.patches = ExitStack()
        self.mock_out_azure_moving_parts()
        self.get_hostname = self.patches.enter_context(
            mock.patch.object(dsaz, 'get_hostname'))
        self.set_hostname = self.patches.enter_context(
            mock.patch.object(dsaz, 'set_hostname'))
        self.subp = self.patches.enter_context(
            mock.patch(MOCKPATH + 'util.subp'))
        self.find_fallback_nic = self.patches.enter_context(
            mock.patch('cloudinit.net.find_fallback_nic', return_value='eth9'))

    def tearDown(self):
        self.patches.close()
        super(TestAzureBounce, self).tearDown()

    def _get_ds(self, ovfcontent=None, agent_command=None):
        if ovfcontent is not None:
            populate_dir(os.path.join(self.paths.seed_dir, "azure"),
                         {'ovf-env.xml': ovfcontent})
        dsrc = dsaz.DataSourceAzure(
            {}, distro=None, paths=self.paths)
        if agent_command is not None:
            dsrc.ds_cfg['agent_command'] = agent_command
        return dsrc

    def _get_and_setup(self, dsrc):
        ret = dsrc.get_data()
        if ret:
            dsrc.setup(True)
        return ret

    def get_ovf_env_with_dscfg(self, hostname, cfg):
        odata = {
            'HostName': hostname,
            'dscfg': {
                'text': b64e(yaml.dump(cfg)),
                'encoding': 'base64'
            }
        }
        return construct_valid_ovf_env(data=odata)

    def test_disabled_bounce_does_not_change_hostname(self):
        cfg = {'hostname_bounce': {'policy': 'off'}}
        ds = self._get_ds(self.get_ovf_env_with_dscfg('test-host', cfg))
        ds.get_data()
        self.assertEqual(0, self.set_hostname.call_count)

    @mock.patch(MOCKPATH + 'perform_hostname_bounce')
    def test_disabled_bounce_does_not_perform_bounce(
            self, perform_hostname_bounce):
        cfg = {'hostname_bounce': {'policy': 'off'}}
        ds = self._get_ds(self.get_ovf_env_with_dscfg('test-host', cfg))
        ds.get_data()
        self.assertEqual(0, perform_hostname_bounce.call_count)

    def test_same_hostname_does_not_change_hostname(self):
        host_name = 'unchanged-host-name'
        self.get_hostname.return_value = host_name
        cfg = {'hostname_bounce': {'policy': 'yes'}}
        ds = self._get_ds(self.get_ovf_env_with_dscfg(host_name, cfg))
        ds.get_data()
        self.assertEqual(0, self.set_hostname.call_count)

    @mock.patch(MOCKPATH + 'perform_hostname_bounce')
    def test_unchanged_hostname_does_not_perform_bounce(
            self, perform_hostname_bounce):
        host_name = 'unchanged-host-name'
        self.get_hostname.return_value = host_name
        cfg = {'hostname_bounce': {'policy': 'yes'}}
        ds = self._get_ds(self.get_ovf_env_with_dscfg(host_name, cfg))
        ds.get_data()
        self.assertEqual(0, perform_hostname_bounce.call_count)

    @mock.patch(MOCKPATH + 'perform_hostname_bounce')
    def test_force_performs_bounce_regardless(self, perform_hostname_bounce):
        host_name = 'unchanged-host-name'
        self.get_hostname.return_value = host_name
        cfg = {'hostname_bounce': {'policy': 'force'}}
        dsrc = self._get_ds(self.get_ovf_env_with_dscfg(host_name, cfg),
                            agent_command=['not', '__builtin__'])
        ret = self._get_and_setup(dsrc)
        self.assertTrue(ret)
        self.assertEqual(1, perform_hostname_bounce.call_count)

    def test_bounce_skipped_on_ifupdown_absent(self):
        host_name = 'unchanged-host-name'
        self.get_hostname.return_value = host_name
        cfg = {'hostname_bounce': {'policy': 'force'}}
        dsrc = self._get_ds(self.get_ovf_env_with_dscfg(host_name, cfg),
                            agent_command=['not', '__builtin__'])
        patch_path = MOCKPATH + 'util.which'
        with mock.patch(patch_path) as m_which:
            m_which.return_value = None
            ret = self._get_and_setup(dsrc)
        self.assertEqual([mock.call('ifup')], m_which.call_args_list)
        self.assertTrue(ret)
        self.assertIn(
            "Skipping network bounce: ifupdown utils aren't present.",
            self.logs.getvalue())

    def test_different_hostnames_sets_hostname(self):
        expected_hostname = 'azure-expected-host-name'
        self.get_hostname.return_value = 'default-host-name'
        dsrc = self._get_ds(
            self.get_ovf_env_with_dscfg(expected_hostname, {}),
            agent_command=['not', '__builtin__'])
        ret = self._get_and_setup(dsrc)
        self.assertTrue(ret)
        self.assertEqual(expected_hostname,
                         self.set_hostname.call_args_list[0][0][0])

    @mock.patch(MOCKPATH + 'perform_hostname_bounce')
    def test_different_hostnames_performs_bounce(
            self, perform_hostname_bounce):
        expected_hostname = 'azure-expected-host-name'
        self.get_hostname.return_value = 'default-host-name'
        dsrc = self._get_ds(
            self.get_ovf_env_with_dscfg(expected_hostname, {}),
            agent_command=['not', '__builtin__'])
        ret = self._get_and_setup(dsrc)
        self.assertTrue(ret)
        self.assertEqual(1, perform_hostname_bounce.call_count)

    def test_different_hostnames_sets_hostname_back(self):
        initial_host_name = 'default-host-name'
        self.get_hostname.return_value = initial_host_name
        dsrc = self._get_ds(
            self.get_ovf_env_with_dscfg('some-host-name', {}),
            agent_command=['not', '__builtin__'])
        ret = self._get_and_setup(dsrc)
        self.assertTrue(ret)
        self.assertEqual(initial_host_name,
                         self.set_hostname.call_args_list[-1][0][0])

    @mock.patch(MOCKPATH + 'perform_hostname_bounce')
    def test_failure_in_bounce_still_resets_host_name(
            self, perform_hostname_bounce):
        perform_hostname_bounce.side_effect = Exception
        initial_host_name = 'default-host-name'
        self.get_hostname.return_value = initial_host_name
        dsrc = self._get_ds(
            self.get_ovf_env_with_dscfg('some-host-name', {}),
            agent_command=['not', '__builtin__'])
        ret = self._get_and_setup(dsrc)
        self.assertTrue(ret)
        self.assertEqual(initial_host_name,
                         self.set_hostname.call_args_list[-1][0][0])

    def test_environment_correct_for_bounce_command(self):
        interface = 'int0'
        hostname = 'my-new-host'
        old_hostname = 'my-old-host'
        self.get_hostname.return_value = old_hostname
        cfg = {'hostname_bounce': {'interface': interface, 'policy': 'force'}}
        data = self.get_ovf_env_with_dscfg(hostname, cfg)
        dsrc = self._get_ds(data, agent_command=['not', '__builtin__'])
        ret = self._get_and_setup(dsrc)
        self.assertTrue(ret)
        self.assertEqual(1, self.subp.call_count)
        bounce_env = self.subp.call_args[1]['env']
        self.assertEqual(interface, bounce_env['interface'])
        self.assertEqual(hostname, bounce_env['hostname'])
        self.assertEqual(old_hostname, bounce_env['old_hostname'])

    def test_default_bounce_command_ifup_used_by_default(self):
        cfg = {'hostname_bounce': {'policy': 'force'}}
        data = self.get_ovf_env_with_dscfg('some-hostname', cfg)
        dsrc = self._get_ds(data, agent_command=['not', '__builtin__'])
        ret = self._get_and_setup(dsrc)
        self.assertTrue(ret)
        self.assertEqual(1, self.subp.call_count)
        bounce_args = self.subp.call_args[1]['args']
        self.assertEqual(
            dsaz.BOUNCE_COMMAND_IFUP, bounce_args)

    @mock.patch(MOCKPATH + 'perform_hostname_bounce')
    def test_set_hostname_option_can_disable_bounce(
            self, perform_hostname_bounce):
        cfg = {'set_hostname': False, 'hostname_bounce': {'policy': 'force'}}
        data = self.get_ovf_env_with_dscfg('some-hostname', cfg)
        self._get_ds(data).get_data()

        self.assertEqual(0, perform_hostname_bounce.call_count)

    def test_set_hostname_option_can_disable_hostname_set(self):
        cfg = {'set_hostname': False, 'hostname_bounce': {'policy': 'force'}}
        data = self.get_ovf_env_with_dscfg('some-hostname', cfg)
        self._get_ds(data).get_data()

        self.assertEqual(0, self.set_hostname.call_count)


class TestLoadAzureDsDir(CiTestCase):
    """Tests for load_azure_ds_dir."""

    def setUp(self):
        self.source_dir = self.tmp_dir()
        super(TestLoadAzureDsDir, self).setUp()

    def test_missing_ovf_env_xml_raises_non_azure_datasource_error(self):
        """load_azure_ds_dir raises an error When ovf-env.xml doesn't exit."""
        with self.assertRaises(dsaz.NonAzureDataSource) as context_manager:
            dsaz.load_azure_ds_dir(self.source_dir)
        self.assertEqual(
            'No ovf-env file found',
            str(context_manager.exception))

    def test_wb_invalid_ovf_env_xml_calls_read_azure_ovf(self):
        """load_azure_ds_dir calls read_azure_ovf to parse the xml."""
        ovf_path = os.path.join(self.source_dir, 'ovf-env.xml')
        with open(ovf_path, 'wb') as stream:
            stream.write(b'invalid xml')
        with self.assertRaises(dsaz.BrokenAzureDataSource) as context_manager:
            dsaz.load_azure_ds_dir(self.source_dir)
        self.assertEqual(
            'Invalid ovf-env.xml: syntax error: line 1, column 0',
            str(context_manager.exception))


class TestReadAzureOvf(CiTestCase):

    def test_invalid_xml_raises_non_azure_ds(self):
        invalid_xml = "<foo>" + construct_valid_ovf_env(data={})
        self.assertRaises(dsaz.BrokenAzureDataSource,
                          dsaz.read_azure_ovf, invalid_xml)

    def test_load_with_pubkeys(self):
        mypklist = [{'fingerprint': 'fp1', 'path': 'path1', 'value': ''}]
        pubkeys = [(x['fingerprint'], x['path'], x['value']) for x in mypklist]
        content = construct_valid_ovf_env(pubkeys=pubkeys)
        (_md, _ud, cfg) = dsaz.read_azure_ovf(content)
        for mypk in mypklist:
            self.assertIn(mypk, cfg['_pubkeys'])


class TestCanDevBeReformatted(CiTestCase):
    warning_file = 'dataloss_warning_readme.txt'

    def _domock(self, mockpath, sattr=None):
        patcher = mock.patch(mockpath)
        setattr(self, sattr, patcher.start())
        self.addCleanup(patcher.stop)

    def patchup(self, devs):
        bypath = {}
        for path, data in devs.items():
            bypath[path] = data
            if 'realpath' in data:
                bypath[data['realpath']] = data
            for ppath, pdata in data.get('partitions', {}).items():
                bypath[ppath] = pdata
                if 'realpath' in data:
                    bypath[pdata['realpath']] = pdata

        def realpath(d):
            return bypath[d].get('realpath', d)

        def partitions_on_device(devpath):
            parts = bypath.get(devpath, {}).get('partitions', {})
            ret = []
            for path, data in parts.items():
                ret.append((data.get('num'), realpath(path)))
            # return sorted by partition number
            return sorted(ret, key=lambda d: d[0])

        def mount_cb(device, callback, mtype, update_env_for_mount):
            self.assertEqual('ntfs', mtype)
            self.assertEqual('C', update_env_for_mount.get('LANG'))
            p = self.tmp_dir()
            for f in bypath.get(device).get('files', []):
                write_file(os.path.join(p, f), content=f)
            return callback(p)

        def has_ntfs_fs(device):
            return bypath.get(device, {}).get('fs') == 'ntfs'

        p = MOCKPATH
        self._domock(p + "_partitions_on_device", 'm_partitions_on_device')
        self._domock(p + "_has_ntfs_filesystem", 'm_has_ntfs_filesystem')
        self._domock(p + "util.mount_cb", 'm_mount_cb')
        self._domock(p + "os.path.realpath", 'm_realpath')
        self._domock(p + "os.path.exists", 'm_exists')
        self._domock(p + "util.SeLinuxGuard", 'm_selguard')

        self.m_exists.side_effect = lambda p: p in bypath
        self.m_realpath.side_effect = realpath
        self.m_has_ntfs_filesystem.side_effect = has_ntfs_fs
        self.m_mount_cb.side_effect = mount_cb
        self.m_partitions_on_device.side_effect = partitions_on_device
        self.m_selguard.__enter__ = mock.Mock(return_value=False)
        self.m_selguard.__exit__ = mock.Mock()

    def test_three_partitions_is_false(self):
        """A disk with 3 partitions can not be formatted."""
        self.patchup({
            '/dev/sda': {
                'partitions': {
                    '/dev/sda1': {'num': 1},
                    '/dev/sda2': {'num': 2},
                    '/dev/sda3': {'num': 3},
                }}})
        value, msg = dsaz.can_dev_be_reformatted("/dev/sda",
                                                 preserve_ntfs=False)
        self.assertFalse(value)
        self.assertIn("3 or more", msg.lower())

    def test_no_partitions_is_false(self):
        """A disk with no partitions can not be formatted."""
        self.patchup({'/dev/sda': {}})
        value, msg = dsaz.can_dev_be_reformatted("/dev/sda",
                                                 preserve_ntfs=False)
        self.assertFalse(value)
        self.assertIn("not partitioned", msg.lower())

    def test_two_partitions_not_ntfs_false(self):
        """2 partitions and 2nd not ntfs can not be formatted."""
        self.patchup({
            '/dev/sda': {
                'partitions': {
                    '/dev/sda1': {'num': 1},
                    '/dev/sda2': {'num': 2, 'fs': 'ext4', 'files': []},
                }}})
        value, msg = dsaz.can_dev_be_reformatted("/dev/sda",
                                                 preserve_ntfs=False)
        self.assertFalse(value)
        self.assertIn("not ntfs", msg.lower())

    def test_two_partitions_ntfs_populated_false(self):
        """2 partitions and populated ntfs fs on 2nd can not be formatted."""
        self.patchup({
            '/dev/sda': {
                'partitions': {
                    '/dev/sda1': {'num': 1},
                    '/dev/sda2': {'num': 2, 'fs': 'ntfs',
                                  'files': ['secret.txt']},
                }}})
        value, msg = dsaz.can_dev_be_reformatted("/dev/sda",
                                                 preserve_ntfs=False)
        self.assertFalse(value)
        self.assertIn("files on it", msg.lower())

    def test_two_partitions_ntfs_empty_is_true(self):
        """2 partitions and empty ntfs fs on 2nd can be formatted."""
        self.patchup({
            '/dev/sda': {
                'partitions': {
                    '/dev/sda1': {'num': 1},
                    '/dev/sda2': {'num': 2, 'fs': 'ntfs', 'files': []},
                }}})
        value, msg = dsaz.can_dev_be_reformatted("/dev/sda",
                                                 preserve_ntfs=False)
        self.assertTrue(value)
        self.assertIn("safe for", msg.lower())

    def test_one_partition_not_ntfs_false(self):
        """1 partition witih fs other than ntfs can not be formatted."""
        self.patchup({
            '/dev/sda': {
                'partitions': {
                    '/dev/sda1': {'num': 1, 'fs': 'zfs'},
                }}})
        value, msg = dsaz.can_dev_be_reformatted("/dev/sda",
                                                 preserve_ntfs=False)
        self.assertFalse(value)
        self.assertIn("not ntfs", msg.lower())

    def test_one_partition_ntfs_populated_false(self):
        """1 mountable ntfs partition with many files can not be formatted."""
        self.patchup({
            '/dev/sda': {
                'partitions': {
                    '/dev/sda1': {'num': 1, 'fs': 'ntfs',
                                  'files': ['file1.txt', 'file2.exe']},
                }}})
        with mock.patch.object(dsaz.LOG, 'warning') as warning:
            value, msg = dsaz.can_dev_be_reformatted("/dev/sda",
                                                     preserve_ntfs=False)
            wmsg = warning.call_args[0][0]
            self.assertIn("looks like you're using NTFS on the ephemeral disk",
                          wmsg)
            self.assertFalse(value)
            self.assertIn("files on it", msg.lower())

    def test_one_partition_ntfs_empty_is_true(self):
        """1 mountable ntfs partition and no files can be formatted."""
        self.patchup({
            '/dev/sda': {
                'partitions': {
                    '/dev/sda1': {'num': 1, 'fs': 'ntfs', 'files': []}
                }}})
        value, msg = dsaz.can_dev_be_reformatted("/dev/sda",
                                                 preserve_ntfs=False)
        self.assertTrue(value)
        self.assertIn("safe for", msg.lower())

    def test_one_partition_ntfs_empty_with_dataloss_file_is_true(self):
        """1 mountable ntfs partition and only warn file can be formatted."""
        self.patchup({
            '/dev/sda': {
                'partitions': {
                    '/dev/sda1': {'num': 1, 'fs': 'ntfs',
                                  'files': ['dataloss_warning_readme.txt']}
                }}})
        value, msg = dsaz.can_dev_be_reformatted("/dev/sda",
                                                 preserve_ntfs=False)
        self.assertTrue(value)
        self.assertIn("safe for", msg.lower())

    def test_one_partition_through_realpath_is_true(self):
        """A symlink to a device with 1 ntfs partition can be formatted."""
        epath = '/dev/disk/cloud/azure_resource'
        self.patchup({
            epath: {
                'realpath': '/dev/sdb',
                'partitions': {
                    epath + '-part1': {
                        'num': 1, 'fs': 'ntfs', 'files': [self.warning_file],
                        'realpath': '/dev/sdb1'}
                }}})
        value, msg = dsaz.can_dev_be_reformatted(epath,
                                                 preserve_ntfs=False)
        self.assertTrue(value)
        self.assertIn("safe for", msg.lower())

    def test_three_partition_through_realpath_is_false(self):
        """A symlink to a device with 3 partitions can not be formatted."""
        epath = '/dev/disk/cloud/azure_resource'
        self.patchup({
            epath: {
                'realpath': '/dev/sdb',
                'partitions': {
                    epath + '-part1': {
                        'num': 1, 'fs': 'ntfs', 'files': [self.warning_file],
                        'realpath': '/dev/sdb1'},
                    epath + '-part2': {'num': 2, 'fs': 'ext3',
                                       'realpath': '/dev/sdb2'},
                    epath + '-part3': {'num': 3, 'fs': 'ext',
                                       'realpath': '/dev/sdb3'}
                }}})
        value, msg = dsaz.can_dev_be_reformatted(epath,
                                                 preserve_ntfs=False)
        self.assertFalse(value)
        self.assertIn("3 or more", msg.lower())

    def test_ntfs_mount_errors_true(self):
        """can_dev_be_reformatted does not fail if NTFS is unknown fstype."""
        self.patchup({
            '/dev/sda': {
                'partitions': {
                    '/dev/sda1': {'num': 1, 'fs': 'ntfs', 'files': []}
                }}})

        error_msgs = [
            "Stderr: mount: unknown filesystem type 'ntfs'",  # RHEL
            "Stderr: mount: /dev/sdb1: unknown filesystem type 'ntfs'"  # SLES
        ]

        for err_msg in error_msgs:
            self.m_mount_cb.side_effect = MountFailedError(
                "Failed mounting %s to %s due to: \nUnexpected.\n%s" %
                ('/dev/sda', '/fake-tmp/dir', err_msg))

            value, msg = dsaz.can_dev_be_reformatted('/dev/sda',
                                                     preserve_ntfs=False)
            self.assertTrue(value)
            self.assertIn('cannot mount NTFS, assuming', msg)

    def test_never_destroy_ntfs_config_false(self):
        """Normally formattable situation with never_destroy_ntfs set."""
        self.patchup({
            '/dev/sda': {
                'partitions': {
                    '/dev/sda1': {'num': 1, 'fs': 'ntfs',
                                  'files': ['dataloss_warning_readme.txt']}
                }}})
        value, msg = dsaz.can_dev_be_reformatted("/dev/sda",
                                                 preserve_ntfs=True)
        self.assertFalse(value)
        self.assertIn("config says to never destroy NTFS "
                      "(datasource.Azure.never_destroy_ntfs)", msg)


class TestClearCachedData(CiTestCase):

    def test_clear_cached_attrs_clears_imds(self):
        """All class attributes are reset to defaults, including imds data."""
        tmp = self.tmp_dir()
        paths = helpers.Paths(
            {'cloud_dir': tmp, 'run_dir': tmp})
        dsrc = dsaz.DataSourceAzure({}, distro=None, paths=paths)
        clean_values = [dsrc.metadata, dsrc.userdata, dsrc._metadata_imds]
        dsrc.metadata = 'md'
        dsrc.userdata = 'ud'
        dsrc._metadata_imds = 'imds'
        dsrc._dirty_cache = True
        dsrc.clear_cached_attrs()
        self.assertEqual(
            [dsrc.metadata, dsrc.userdata, dsrc._metadata_imds],
            clean_values)


class TestAzureNetExists(CiTestCase):

    def test_azure_net_must_exist_for_legacy_objpkl(self):
        """DataSourceAzureNet must exist for old obj.pkl files
           that reference it."""
        self.assertTrue(hasattr(dsaz, "DataSourceAzureNet"))


class TestPreprovisioningReadAzureOvfFlag(CiTestCase):

    def test_read_azure_ovf_with_true_flag(self):
        """The read_azure_ovf method should set the PreprovisionedVM
           cfg flag if the proper setting is present."""
        content = construct_valid_ovf_env(
            platform_settings={"PreprovisionedVm": "True"})
        ret = dsaz.read_azure_ovf(content)
        cfg = ret[2]
        self.assertTrue(cfg['PreprovisionedVm'])

    def test_read_azure_ovf_with_false_flag(self):
        """The read_azure_ovf method should set the PreprovisionedVM
           cfg flag to false if the proper setting is false."""
        content = construct_valid_ovf_env(
            platform_settings={"PreprovisionedVm": "False"})
        ret = dsaz.read_azure_ovf(content)
        cfg = ret[2]
        self.assertFalse(cfg['PreprovisionedVm'])

    def test_read_azure_ovf_without_flag(self):
        """The read_azure_ovf method should not set the
           PreprovisionedVM cfg flag."""
        content = construct_valid_ovf_env()
        ret = dsaz.read_azure_ovf(content)
        cfg = ret[2]
        self.assertFalse(cfg['PreprovisionedVm'])


@mock.patch('os.path.isfile')
class TestPreprovisioningShouldReprovision(CiTestCase):

    def setUp(self):
        super(TestPreprovisioningShouldReprovision, self).setUp()
        tmp = self.tmp_dir()
        self.waagent_d = self.tmp_path('/var/lib/waagent', tmp)
        self.paths = helpers.Paths({'cloud_dir': tmp})
        dsaz.BUILTIN_DS_CONFIG['data_dir'] = self.waagent_d

    @mock.patch(MOCKPATH + 'util.write_file')
    def test__should_reprovision_with_true_cfg(self, isfile, write_f):
        """The _should_reprovision method should return true with config
           flag present."""
        isfile.return_value = False
        dsa = dsaz.DataSourceAzure({}, distro=None, paths=self.paths)
        self.assertTrue(dsa._should_reprovision(
            (None, None, {'PreprovisionedVm': True}, None)))

    def test__should_reprovision_with_file_existing(self, isfile):
        """The _should_reprovision method should return True if the sentinal
           exists."""
        isfile.return_value = True
        dsa = dsaz.DataSourceAzure({}, distro=None, paths=self.paths)
        self.assertTrue(dsa._should_reprovision(
            (None, None, {'preprovisionedvm': False}, None)))

    def test__should_reprovision_returns_false(self, isfile):
        """The _should_reprovision method should return False
           if config and sentinal are not present."""
        isfile.return_value = False
        dsa = dsaz.DataSourceAzure({}, distro=None, paths=self.paths)
        self.assertFalse(dsa._should_reprovision((None, None, {}, None)))

    @mock.patch(MOCKPATH + 'DataSourceAzure._poll_imds')
    def test_reprovision_calls__poll_imds(self, _poll_imds, isfile):
        """_reprovision will poll IMDS."""
        isfile.return_value = False
        hostname = "myhost"
        username = "myuser"
        odata = {'HostName': hostname, 'UserName': username}
        _poll_imds.return_value = construct_valid_ovf_env(data=odata)
        dsa = dsaz.DataSourceAzure({}, distro=None, paths=self.paths)
        dsa._reprovision()
        _poll_imds.assert_called_with()


@mock.patch('cloudinit.net.dhcp.EphemeralIPv4Network')
@mock.patch('cloudinit.net.dhcp.maybe_perform_dhcp_discovery')
@mock.patch('cloudinit.sources.helpers.netlink.'
            'wait_for_media_disconnect_connect')
@mock.patch('requests.Session.request')
@mock.patch(MOCKPATH + 'DataSourceAzure._report_ready')
class TestPreprovisioningPollIMDS(CiTestCase):

    def setUp(self):
        super(TestPreprovisioningPollIMDS, self).setUp()
        self.tmp = self.tmp_dir()
        self.waagent_d = self.tmp_path('/var/lib/waagent', self.tmp)
        self.paths = helpers.Paths({'cloud_dir': self.tmp})
        dsaz.BUILTIN_DS_CONFIG['data_dir'] = self.waagent_d

    @mock.patch('time.sleep', mock.MagicMock())
    @mock.patch(MOCKPATH + 'EphemeralDHCPv4')
    def test_poll_imds_re_dhcp_on_timeout(self, m_dhcpv4, report_ready_func,
                                          fake_resp, m_media_switch, m_dhcp,
                                          m_net):
        """The poll_imds will retry DHCP on IMDS timeout."""
        report_file = self.tmp_path('report_marker', self.tmp)
        lease = {
            'interface': 'eth9', 'fixed-address': '192.168.2.9',
            'routers': '192.168.2.1', 'subnet-mask': '255.255.255.0',
            'unknown-245': '624c3620'}
        m_dhcp.return_value = [lease]
        m_media_switch.return_value = None
        dhcp_ctx = mock.MagicMock(lease=lease)
        dhcp_ctx.obtain_lease.return_value = lease
        m_dhcpv4.return_value = dhcp_ctx

        self.tries = 0

        def fake_timeout_once(**kwargs):
            self.tries += 1
            if self.tries == 1:
                raise requests.Timeout('Fake connection timeout')
            elif self.tries == 2:
                response = requests.Response()
                response.status_code = 404
                raise requests.exceptions.HTTPError(
                    "fake 404", response=response)
            # Third try should succeed and stop retries or redhcp
            return mock.MagicMock(status_code=200, text="good", content="good")

        fake_resp.side_effect = fake_timeout_once

        dsa = dsaz.DataSourceAzure({}, distro=None, paths=self.paths)
        with mock.patch(MOCKPATH + 'REPORTED_READY_MARKER_FILE', report_file):
            dsa._poll_imds()
        self.assertEqual(report_ready_func.call_count, 1)
        report_ready_func.assert_called_with(lease=lease)
        self.assertEqual(3, m_dhcpv4.call_count, 'Expected 3 DHCP calls')
        self.assertEqual(3, self.tries, 'Expected 3 total reads from IMDS')

    def test_poll_imds_report_ready_false(self,
                                          report_ready_func, fake_resp,
                                          m_media_switch, m_dhcp, m_net):
        """The poll_imds should not call reporting ready
           when flag is false"""
        report_file = self.tmp_path('report_marker', self.tmp)
        write_file(report_file, content='dont run report_ready :)')
        m_dhcp.return_value = [{
            'interface': 'eth9', 'fixed-address': '192.168.2.9',
            'routers': '192.168.2.1', 'subnet-mask': '255.255.255.0',
            'unknown-245': '624c3620'}]
        m_media_switch.return_value = None
        dsa = dsaz.DataSourceAzure({}, distro=None, paths=self.paths)
        with mock.patch(MOCKPATH + 'REPORTED_READY_MARKER_FILE', report_file):
            dsa._poll_imds()
        self.assertEqual(report_ready_func.call_count, 0)


@mock.patch(MOCKPATH + 'util.subp')
@mock.patch(MOCKPATH + 'util.write_file')
@mock.patch(MOCKPATH + 'util.is_FreeBSD')
@mock.patch('cloudinit.sources.helpers.netlink.'
            'wait_for_media_disconnect_connect')
@mock.patch('cloudinit.net.dhcp.EphemeralIPv4Network')
@mock.patch('cloudinit.net.dhcp.maybe_perform_dhcp_discovery')
@mock.patch('requests.Session.request')
class TestAzureDataSourcePreprovisioning(CiTestCase):

    def setUp(self):
        super(TestAzureDataSourcePreprovisioning, self).setUp()
        tmp = self.tmp_dir()
        self.waagent_d = self.tmp_path('/var/lib/waagent', tmp)
        self.paths = helpers.Paths({'cloud_dir': tmp})
        dsaz.BUILTIN_DS_CONFIG['data_dir'] = self.waagent_d

    def test_poll_imds_returns_ovf_env(self, fake_resp,
                                       m_dhcp, m_net,
                                       m_media_switch,
                                       m_is_bsd, write_f, subp):
        """The _poll_imds method should return the ovf_env.xml."""
        m_is_bsd.return_value = False
        m_media_switch.return_value = None
        m_dhcp.return_value = [{
            'interface': 'eth9', 'fixed-address': '192.168.2.9',
            'routers': '192.168.2.1', 'subnet-mask': '255.255.255.0'}]
        url = 'http://{0}/metadata/reprovisiondata?api-version=2017-04-02'
        host = "169.254.169.254"
        full_url = url.format(host)
        fake_resp.return_value = mock.MagicMock(status_code=200, text="ovf",
                                                content="ovf")
        dsa = dsaz.DataSourceAzure({}, distro=None, paths=self.paths)
        self.assertTrue(len(dsa._poll_imds()) > 0)
        self.assertEqual(fake_resp.call_args_list,
                         [mock.call(allow_redirects=True,
                                    headers={'Metadata': 'true',
                                             'User-Agent':
                                             'Cloud-Init/%s' % vs()
                                             }, method='GET',
                                    timeout=dsaz.IMDS_TIMEOUT_IN_SECONDS,
                                    url=full_url)])
        self.assertEqual(m_dhcp.call_count, 2)
        m_net.assert_any_call(
            broadcast='192.168.2.255', interface='eth9', ip='192.168.2.9',
            prefix_or_mask='255.255.255.0', router='192.168.2.1',
            static_routes=None)
        self.assertEqual(m_net.call_count, 2)

    def test__reprovision_calls__poll_imds(self, fake_resp,
                                           m_dhcp, m_net,
                                           m_media_switch,
                                           m_is_bsd, write_f, subp):
        """The _reprovision method should call poll IMDS."""
        m_is_bsd.return_value = False
        m_media_switch.return_value = None
        m_dhcp.return_value = [{
            'interface': 'eth9', 'fixed-address': '192.168.2.9',
            'routers': '192.168.2.1', 'subnet-mask': '255.255.255.0',
            'unknown-245': '624c3620'}]
        url = 'http://{0}/metadata/reprovisiondata?api-version=2017-04-02'
        host = "169.254.169.254"
        full_url = url.format(host)
        hostname = "myhost"
        username = "myuser"
        odata = {'HostName': hostname, 'UserName': username}
        content = construct_valid_ovf_env(data=odata)
        fake_resp.return_value = mock.MagicMock(status_code=200, text=content,
                                                content=content)
        dsa = dsaz.DataSourceAzure({}, distro=None, paths=self.paths)
        md, _ud, cfg, _d = dsa._reprovision()
        self.assertEqual(md['local-hostname'], hostname)
        self.assertEqual(cfg['system_info']['default_user']['name'], username)
        self.assertEqual(fake_resp.call_args_list,
                         [mock.call(allow_redirects=True,
                                    headers={'Metadata': 'true',
                                             'User-Agent':
                                             'Cloud-Init/%s' % vs()},
                                    method='GET',
                                    timeout=dsaz.IMDS_TIMEOUT_IN_SECONDS,
                                    url=full_url)])
        self.assertEqual(m_dhcp.call_count, 2)
        m_net.assert_any_call(
            broadcast='192.168.2.255', interface='eth9', ip='192.168.2.9',
            prefix_or_mask='255.255.255.0', router='192.168.2.1',
            static_routes=None)
        self.assertEqual(m_net.call_count, 2)


class TestRemoveUbuntuNetworkConfigScripts(CiTestCase):

    with_logs = True

    def setUp(self):
        super(TestRemoveUbuntuNetworkConfigScripts, self).setUp()
        self.tmp = self.tmp_dir()

    def test_remove_network_scripts_removes_both_files_and_directories(self):
        """Any files or directories in paths are removed when present."""
        file1 = self.tmp_path('file1', dir=self.tmp)
        subdir = self.tmp_path('sub1', dir=self.tmp)
        subfile = self.tmp_path('leaf1', dir=subdir)
        write_file(file1, 'file1content')
        write_file(subfile, 'leafcontent')
        dsaz.maybe_remove_ubuntu_network_config_scripts(paths=[subdir, file1])

        for path in (file1, subdir, subfile):
            self.assertFalse(os.path.exists(path),
                             'Found unremoved: %s' % path)

        expected_logs = [
            'INFO: Removing Ubuntu extended network scripts because cloud-init'
            ' updates Azure network configuration on the following event:'
            ' System boot.',
            'Recursively deleting %s' % subdir,
            'Attempting to remove %s' % file1]
        for log in expected_logs:
            self.assertIn(log, self.logs.getvalue())

    def test_remove_network_scripts_only_attempts_removal_if_path_exists(self):
        """Any files or directories absent are skipped without error."""
        dsaz.maybe_remove_ubuntu_network_config_scripts(paths=[
            self.tmp_path('nodirhere/', dir=self.tmp),
            self.tmp_path('notfilehere', dir=self.tmp)])
        self.assertNotIn('/not/a', self.logs.getvalue())  # No delete logs

    @mock.patch(MOCKPATH + 'os.path.exists')
    def test_remove_network_scripts_default_removes_stock_scripts(self,
                                                                  m_exists):
        """Azure's stock ubuntu image scripts and artifacts are removed."""
        # Report path absent on all to avoid delete operation
        m_exists.return_value = False
        dsaz.maybe_remove_ubuntu_network_config_scripts()
        calls = m_exists.call_args_list
        for path in dsaz.UBUNTU_EXTENDED_NETWORK_SCRIPTS:
            self.assertIn(mock.call(path), calls)


class TestWBIsPlatformViable(CiTestCase):
    """White box tests for _is_platform_viable."""
    with_logs = True

    @mock.patch(MOCKPATH + 'util.read_dmi_data')
    def test_true_on_non_azure_chassis(self, m_read_dmi_data):
        """Return True if DMI chassis-asset-tag is AZURE_CHASSIS_ASSET_TAG."""
        m_read_dmi_data.return_value = dsaz.AZURE_CHASSIS_ASSET_TAG
        self.assertTrue(dsaz._is_platform_viable('doesnotmatter'))

    @mock.patch(MOCKPATH + 'os.path.exists')
    @mock.patch(MOCKPATH + 'util.read_dmi_data')
    def test_true_on_azure_ovf_env_in_seed_dir(self, m_read_dmi_data, m_exist):
        """Return True if ovf-env.xml exists in known seed dirs."""
        # Non-matching Azure chassis-asset-tag
        m_read_dmi_data.return_value = dsaz.AZURE_CHASSIS_ASSET_TAG + 'X'

        m_exist.return_value = True
        self.assertTrue(dsaz._is_platform_viable('/some/seed/dir'))
        m_exist.called_once_with('/other/seed/dir')

    def test_false_on_no_matching_azure_criteria(self):
        """Report non-azure on unmatched asset tag, ovf-env absent and no dev.

        Return False when the asset tag doesn't match Azure's static
        AZURE_CHASSIS_ASSET_TAG, no ovf-env.xml files exist in known seed dirs
        and no devices have a label starting with prefix 'rd_rdfe_'.
        """
        self.assertFalse(wrap_and_call(
            MOCKPATH,
            {'os.path.exists': False,
             # Non-matching Azure chassis-asset-tag
             'util.read_dmi_data': dsaz.AZURE_CHASSIS_ASSET_TAG + 'X',
             'util.which': None},
            dsaz._is_platform_viable, 'doesnotmatter'))
        self.assertIn(
            "DEBUG: Non-Azure DMI asset tag '{0}' discovered.\n".format(
                dsaz.AZURE_CHASSIS_ASSET_TAG + 'X'),
            self.logs.getvalue())


class TestRandomSeed(CiTestCase):
    """Test proper handling of random_seed"""

    def test_non_ascii_seed_is_serializable(self):
        """Pass if a random string from the Azure infrastructure which
        contains at least one non-Unicode character can be converted to/from
        JSON without alteration and without throwing an exception.
        """
        path = resourceLocation("azure/non_unicode_random_string")
        result = dsaz._get_random_seed(path)

        obj = {'seed': result}
        try:
            serialized = json_dumps(obj)
            deserialized = load_json(serialized)
        except UnicodeDecodeError:
            self.fail("Non-serializable random seed returned")

        self.assertEqual(deserialized['seed'], result)

# vi: ts=4 expandtab
