########
# Copyright (c) 2014 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
#    * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    * See the License for the specific language governing permissions and
#    * limitations under the License.

__author__ = 'boris'

import random
import logging
import os
import time
import copy
from contextlib import contextmanager

import novaclient.v1_1.client as nvclient
from retrying import retry

from cosmo_tester.framework.handlers import BaseHandler
from cosmo_tester.framework.util import get_actual_keypath


logging.getLogger('novaclient.client').setLevel(logging.INFO)

CLOUDIFY_TEST_NO_CLEANUP = 'CLOUDIFY_TEST_NO_CLEANUP'


def openstack_clients(cloudify_config):
    creds = _client_creds(cloudify_config)
    return nvclient.Client(**creds)


@retry(stop_max_attempt_number=5, wait_fixed=20000)
def openstack_infra_state(cloudify_config):
    nova = openstack_clients(cloudify_config)
    config_reader = CloudifyOpenstackNovaNetConfigReader(cloudify_config)
    prefix = config_reader.resources_prefix
    return {
        'security_groups': dict(_security_groups(nova, prefix)),
        'servers': dict(_servers(nova, prefix)),
        'key_pairs': dict(_key_pairs(nova, prefix)),
        'floatingips': dict(_floatingips(nova, prefix)),
    }


def remove_openstack_resources(cloudify_config, resources_to_remove):
    # basically sort of a workaround, but if we get the order wrong
    # the first time, there is a chance things would better next time
    # 3'rd time can't really hurt, can it?
    # 3 is a charm
    for _ in range(3):
        resources_to_remove = _remove_openstack_resources_impl(
            cloudify_config, resources_to_remove)
        if all([len(g) == 0 for g in resources_to_remove.values()]):
            break
        # give openstack some time to update its data structures
        time.sleep(3)
    return resources_to_remove


def _remove_openstack_resources_impl(cloudify_config,
                                     resources_to_remove):
    nova = openstack_clients(cloudify_config)
    servers = nova.servers.list()
    keypairs = nova.keypairs.list()
    floatingips = nova.floating_ips.list()
    security_groups = nova.security_groups.list()

    failed = {
        'key_pairs': {},
        'floatingips': {},
        'security_groups': {}
    }

    for server in servers:
        if server.id in resources_to_remove['servers']:
            with _handled_exception(server.id, failed, 'servers'):
                nova.servers.delete(server)
    for key_pair in keypairs:
        if key_pair.id in resources_to_remove['key_pairs']:
            with _handled_exception(key_pair.id, failed, 'key_pairs'):
                nova.keypairs.delete(key_pair)
    for floatingip in floatingips:
        if floatingip['id'] in resources_to_remove['floatingips']:
            with _handled_exception(floatingip['id'], failed, 'floatingips'):
                nova.floating_ips.delete(floatingip['id'])
    for security_group in security_groups:
        if security_group['name'] == 'default':
            continue
        if security_group['id'] in resources_to_remove['security_groups']:
            with _handled_exception(security_group['id'],
                                    failed, 'security_groups'):
                nova.security_groups.delete(security_group['id'])

    return failed


def openstack_infra_state_delta(before, after):
    after = copy.deepcopy(after)
    return {
        prop: _remove_keys(after[prop], before[prop].keys())
        for prop in before.keys()
    }


def _client_creds(cloudify_config):
    return {
        'username': cloudify_config['keystone']['username'],
        'api_key': cloudify_config['keystone']['password'],
        'auth_url': cloudify_config['keystone']['auth_url'],
        'project_id': cloudify_config['keystone']['tenant_name'],
        'region_name': cloudify_config['compute']['region']
    }


def _security_groups(nova, prefix):
    return [(n['id'], n['name'])
            for n in nova.security_groups.list()
            if _check_prefix(n['name'], prefix)]


def _servers(nova, prefix):
    return [(s.id, s.human_id)
            for s in nova.servers.list()
            if _check_prefix(s.human_id, prefix)]


def _key_pairs(nova, prefix):
    return [(kp.id, kp.name)
            for kp in nova.keypairs.list()
            if _check_prefix(kp.name, prefix)]


def _floatingips(nova, prefix):
    return [(ip['id'], ip['floating_ip_address'])
            for ip in nova.floating_ips.list()]
    # return []


def _check_prefix(name, prefix):
    return name.startswith(prefix)


def _remove_keys(dct, keys):
    for key in keys:
        if key in dct:
            del dct[key]
    return dct


@contextmanager
def _handled_exception(resource_id, failed, resource_group):
    try:
        yield
    except BaseException, ex:
        failed[resource_group][resource_id] = ex


class OpenstackNovaNetCleanupContext(BaseHandler.CleanupContext):

    def __init__(self, context_name, cloudify_config):
        super(OpenstackNovaNetCleanupContext, self).__init__(context_name,
                                                             cloudify_config)
        self.before_run = openstack_infra_state(cloudify_config)

    def cleanup(self):
        super(OpenstackNovaNetCleanupContext, self).cleanup()
        resources_to_teardown = self.get_resources_to_teardown()
        if os.environ.get(CLOUDIFY_TEST_NO_CLEANUP):
            self.logger.warn('[{0}] SKIPPING cleanup: of the resources: {1}'
                             .format(self.context_name, resources_to_teardown))
            return
        self.logger.info('[{0}] Performing cleanup: will try removing these '
                         'resources: {1}'
                         .format(self.context_name, resources_to_teardown))

        leftovers = remove_openstack_resources(self.cloudify_config,
                                               resources_to_teardown)
        self.logger.info('[{0}] Leftover resources after cleanup: {1}'
                         .format(self.context_name, leftovers))

    def get_resources_to_teardown(self):
        current_state = openstack_infra_state(self.cloudify_config)
        return openstack_infra_state_delta(before=self.before_run,
                                           after=current_state)


class CloudifyOpenstackNovaNetConfigReader(BaseHandler.CloudifyConfigReader):

    def __init__(self, cloudify_config):
        super(CloudifyOpenstackNovaNetConfigReader, self).\
            __init__(cloudify_config)

    @property
    def region(self):
        return self.config['compute']['region']

    @property
    def management_server_name(self):
        return self.config['compute']['management_server']['instance']['name']

    @property
    def management_server_floating_ip(self):
        return self.config['compute']['management_server']['floating_ip']

    @property
    def agent_key_path(self):
        return self.config['compute']['agent_servers']['agents_keypair'][
            'private_key_path']

    @property
    def managment_user_name(self):
        return self.config['compute']['management_server'][
            'user_on_management']

    @property
    def management_key_path(self):
        return self.config['compute']['management_server'][
            'management_keypair']['private_key_path']

    @property
    def agent_keypair_name(self):
        return self.config['compute']['agent_servers']['agents_keypair'][
            'name']

    @property
    def management_keypair_name(self):
        return self.config['compute']['management_server'][
            'management_keypair']['name']

    @property
    def agents_security_group(self):
        return self.config['networking']['agents_security_group']['name']

    @property
    def management_security_group(self):
        return self.config['networking']['management_security_group']['name']


class OpenstackNovaNetHandler(BaseHandler):

    provider = 'openstack'
    CleanupContext = OpenstackNovaNetCleanupContext
    CloudifyConfigReader = CloudifyOpenstackNovaNetConfigReader

    ubuntu_image_name = 'Ubuntu 12.04'
    flavor_name = 'm1.small'
    ubuntu_image_id = '0b9f53c8-e5dc-495a-87cc-c9654c29e2e5'
    small_flavor_id = 2

    def before_bootstrap(self):
        with self.update_cloudify_config() as patch:
            suffix = '-%06x' % random.randrange(16 ** 6)
            patch.append_value('compute.management_server.instance.name',
                               suffix)

    def after_bootstrap(self, provider_context):
        resources = provider_context['resources']
        agent_keypair = resources['agents_keypair']
        management_keypair = resources['management_keypair']
        self.remove_agent_keypair = agent_keypair['external_resource'] is False
        self.remove_management_keypair = \
            management_keypair['external_resource'] is False

    def after_teardown(self):
        if self.remove_agent_keypair:
            agent_key_path = get_actual_keypath(self.env,
                                                self.env.agent_key_path,
                                                raise_on_missing=False)
            if agent_key_path:
                os.remove(agent_key_path)
        if self.remove_management_keypair:
            management_key_path = get_actual_keypath(
                self.env,
                self.env.management_key_path,
                raise_on_missing=False)
            if management_key_path:
                os.remove(management_key_path)

handler = OpenstackNovaNetHandler
