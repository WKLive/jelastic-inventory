#!/usr/bin/env python


# (c) 2015, Colin Campbell, Wunderkaut Sweden AB
# (c) 2012 Peter Sankauskas
#
# This software is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This software is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this software.  If not, see <http://www.gnu.org/licenses/>.


'''
Jelastic EasyPaaS external inventory script
=================================

    export JELASTIC_INI_PATH=/path/to/jelastic.ini
    export JELASTIC_USER_ID=<user id>
    export JELASTIC_USER_PASSWORD=<user password>
    export JELASTIC_APP_URL=<jelastic url>

'''

import sys
import os
import argparse
import re
from time import time
import six
import urllib
from contextlib import closing
from six.moves import configparser

# from collections import defaultdict

try:
    import json
except ImportError:
    import simplejson as json


class JelasticInventory(object):
    @staticmethod
    def _empty_inventory():
        return {"_meta": {"hostvars": {}}}

    def __init__(self):

        self.inventory = self._empty_inventory()

        # Index of hostname (address) to instance ID.
        self.index = {}

        # Attributes initialized in read_settings() and parse_cli_args().
        self.session = None
        # self.pattern_exclude = None
        # self.pattern_include = None
        self.app_url = None
        self.app_id = None
        self.cache_path_cache = None
        self.cache_path_index = None
        self.cache_max_age = None
        # self.nested_groups = None
        self.container_mapping = None
        self.jelastic_ssh_gateway = None
        self.jelastic_ssh_port = None

        # Read settings and parse CLI arguments
        self.read_settings()
        self.parse_cli_args()

        # Cache
        if self.args.refresh_cache:
            self.do_api_calls_update_cache()
        elif not self.is_cache_valid():
            self.do_api_calls_update_cache()

        # Data to print
        if self.args.host:
            data_to_print = self.get_host_info()

        elif self.args.list:
            # Display list of instances for inventory
            if self.inventory == self._empty_inventory():
                data_to_print = self.get_inventory_from_cache()
            else:
                data_to_print = self.json_format_dict(self.inventory, True)

        print(data_to_print)

    def login(self):
        username = os.environ.get('JELASTIC_USER_ID')
        password = os.environ.get('JELASTIC_USER_PASSWORD')
        if username is None or password is None:
            self.fail_with_error('Username or password not set', 'login')

        payload = {'appid': self.app_id, 'login': username, 'password': password}

        try:
            with closing(urllib.urlopen(self.app_url + '/users/authentication/rest/signin?' + urllib.urlencode(
                    payload))) as response:
                result = json.load(response)
                if result['result'] != 0:
                    self.fail_with_error("Jelastic API " + result['error'])

                self.session = result
        except urllib.error.URLError as e:
            self.fail_with_error(e.reason, 'login')

    def logout(self):
        payload = {'appid': self.app_id, 'session': self.session['session']}
        try:
            with closing(urllib.urlopen(self.app_url + '/users/authentication/rest/signout?' + urllib.urlencode(
                    payload))) as response:
                result = json.load(response)
                if result['result'] != 0:
                    self.fail_with_error("Error Logging out of Jelastic API " + result['error'])
                self.session = result
        except:
            e = sys.exc_info()[0]
            self.fail_with_error(e.reason, 'logout')

    def is_cache_valid(self):
        # Determines if the cache files have expired, or if it is still valid.
        if os.path.isfile(self.cache_path_cache):
            mod_time = os.path.getmtime(self.cache_path_cache)
            current_time = time()
            if (mod_time + self.cache_max_age) > current_time:
                if os.path.isfile(self.cache_path_index):
                    return True

        return False

    def read_settings(self):
        # Reads the settings from the jelastic.ini file.
        if six.PY3:
            config = configparser.ConfigParser()
        else:
            config = configparser.SafeConfigParser()

        jelastic_default_ini_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'jelastic.ini')
        jelastic_ini_path = os.path.expanduser(
            os.path.expandvars(os.environ.get('JELASTIC_INI_PATH', jelastic_default_ini_path)))
        config.read(jelastic_ini_path)

        # Jelastic provider settings
        self.app_url = os.environ.get('JELASTIC_APP_URL', config.get('jelastic', 'app_url'))
        self.app_id = config.get('jelastic', 'app_id')

        # Cache related
        cache_dir = os.path.expanduser(config.get('jelastic', 'cache_path'))
        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir)

        self.cache_path_cache = cache_dir + "/ansible-jelastic.cache"
        self.cache_path_index = cache_dir + "/ansible-jelastic.index"
        self.cache_max_age = config.getint('jelastic', 'cache_max_age')

        if config.has_option('jelastic', 'jelastic_ssh_gateway'):
            self.jelastic_ssh_gateway = config.get('jelastic', 'jelastic_ssh_gateway')
        else:
            self.jelastic_ssh_gateway = 'localhost'

        if config.has_option('jelastic', 'jelastic_ssh_port'):
            self.jelastic_ssh_port = config.get('jelastic', 'jelastic_ssh_port')
        else:
            self.jelastic_ssh_port = '22'

        # Configure which groups should be created.
        group_by_options = [
            'group_by_environment_id',
            'group_by_node_type',
            'group_by_node_class'
        ]
        for option in group_by_options:
            if config.has_option('jelastic', option):
                setattr(self, option, config.getboolean('jelastic', option))
            else:
                setattr(self, option, True)

        self.container_mapping = dict(config.items('container_mapping'))

    def parse_cli_args(self):
        # Command line argument processing

        parser = argparse.ArgumentParser(description='Produce an Ansible Inventory file based on Jelastic EasyPaaS')
        parser.add_argument('--list', action='store_true', default=True,
                            help='List instances (default: True)')
        parser.add_argument('--host', action='store',
                            help='Get all the variables about a specific instance')
        parser.add_argument('--refresh-cache', action='store_true', default=False,
                            help='Force refresh of cache by making API requests to Jelastic (default: False - use cache files)')
        self.args = parser.parse_args()


    def do_api_calls_update_cache(self):
        # Do API call, and save data in cache files

        self.get_environments()

        self.write_to_cache(self.inventory, self.cache_path_cache)
        self.write_to_cache(self.index, self.cache_path_index)

    def get_environments(self):
        self.login()
        payload = {'appid': self.app_id, 'session': self.session['session']}
        try:
            with closing(urllib.urlopen(self.app_url + '/environment/environment/rest/getenvs?' + urllib.urlencode(
                    payload))) as response:
                result = json.load(response)
                if result['result'] != 0:
                    self.fail_with_error("Jelastic API " + result['error'])
            for environment in result['infos']:
                self.add_environment(environment)
        except:
            e = sys.exc_info()[0]
            self.fail_with_error(e.reason, 'login')
        finally:
            self.logout();

    @staticmethod
    def fail_with_error(err_msg, err_operation=None):
        '''log an error to std err for ansible-playbook to consume and exit'''
        if err_operation:
            err_msg = 'ERROR: "{err_msg}", while: {err_operation}'.format(
                err_msg=err_msg, err_operation=err_operation)
        sys.stderr.write(err_msg)
        sys.exit(1)

    def map_node_class(self, nodeType):
        matches = [val for key, val in self.container_mapping.iteritems() if nodeType.startswith(key)]
        if not matches:
            return 'unknown'
        return matches[0]

    def add_environment(self, environment):
        # Add vars for environment here, then add nodes
        if environment['env']['status'] != 1:
            return

        for node in environment['nodes']:
            self.add_node(environment['env'], node)

    def add_node(self, env, node):
        dest = node['address']
        if dest is None:
            return
        self.index[dest] = [env['domain'], node['id']]
        self.push(self.inventory, env['domain'], dest)
        self.push(self.inventory, env['shortdomain'], dest)

        if self.group_by_node_type:
            self.push(self.inventory, node['nodeType'], dest)

        if self.group_by_node_class:
            self.push(self.inventory, self.map_node_class(node['nodeType']), dest)

        self.inventory["_meta"]["hostvars"][dest] = self.get_node_hostvars(env, node)

    def get_node_hostvars(self, env, node):
        node_vars = {}
        node_vars['ansible_ssh_user'] = "{}-{}".format(node['id'], env['uid'])
        node_vars['ansible_ssh_host'] = self.jelastic_ssh_gateway
        node_vars['ansible_ssh_port'] = self.jelastic_ssh_port
        # Todo: other node properties as needed.
        return node_vars

    def get_host_info(self):
        ''' Get variables about a specific host '''

        if len(self.index) == 0:
            # Need to load index from cache
            self.load_index_from_cache()

        if not self.args.host in self.index:
            # try updating the cache
            self.do_api_calls_update_cache()
            if not self.args.host in self.index:
                # host might not exist anymore
                return self.json_format_dict({}, True)

        (environment, node_id) = self.index[self.args.host]

        node = self.get_node(environment, node_id)
        return self.json_format_dict(self.get_host_info_dict_from_node(node), True)

    def get_host_info_dict_from_node(self, node):
        return

    def push(self, my_dict, key, element):
        ''' Push an element onto an array that may not have been defined in
        the dict '''
        group_info = my_dict.setdefault(key, [])
        if isinstance(group_info, dict):
            host_list = group_info.setdefault('hosts', [])
            host_list.append(element)
        else:
            group_info.append(element)

    def get_inventory_from_cache(self):
        ''' Reads the inventory from the cache file and returns it as a JSON
        object '''

        cache = open(self.cache_path_cache, 'r')
        json_inventory = cache.read()
        return json_inventory

    def load_index_from_cache(self):
        ''' Reads the index from the cache file sets self.index '''

        cache = open(self.cache_path_index, 'r')
        json_index = cache.read()
        self.index = json.loads(json_index)

    def write_to_cache(self, data, filename):
        ''' Writes data in JSON format to a file '''

        json_data = self.json_format_dict(data, True)
        cache = open(filename, 'w')
        cache.write(json_data)
        cache.close()

    def json_format_dict(self, data, pretty=False):
        ''' Converts a dict to a JSON object and dumps it as a formatted
        string '''

        if pretty:
            return json.dumps(data, sort_keys=True, indent=2)
        else:
            return json.dumps(data)


# Run the script
JelasticInventory()
