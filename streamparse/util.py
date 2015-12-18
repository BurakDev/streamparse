from __future__ import absolute_import, print_function, unicode_literals

import os
import re
import shutil
import sys
from glob import glob
from os.path import join
from random import shuffle

import requests
import simplejson as json
from fabric.api import env
from fabric.colors import red
from invoke import run
from pkg_resources import parse_version
from prettytable import PrettyTable
from six import iteritems
from thriftpy.protocol import TBinaryProtocolFactory
from thriftpy.rpc import make_client
from thriftpy.transport import TFramedTransportFactory

from .contextmanagers import ssh_tunnel
from .decorators import memoized
from .thrift import storm_thrift


def activate_env(env_name=None):
    """Activate a particular environment from a streamparse project's
    config.json file and populatefabric's env dictionary with appropriate
    values.

    :param env_name: a `str` corresponding to the key within the config file's
                     "envs" dictionary.
    """
    env_name, env_config = get_env_config(env_name)

    env.storm_workers = env_config["workers"]
    env.user = env_config.get("user")
    env.log_path = env_config.get("log_path") or \
                   env_config.get("log", {}).get("path")
    env.virtualenv_root = env_config.get("virtualenv_root") or \
                          env_config.get("virtualenv_path")
    env.disable_known_hosts = True
    env.forward_agent = True
    env.use_ssh_config = True


def die(msg, error_code=1):
    print("{}: {}".format(red("error"), msg))
    sys.exit(error_code)


@memoized
def get_config():
    if not os.path.exists("config.json"):
        die("No config.json found. You must run this command inside a "
            "streamparse project directory.")

    with open("config.json") as fp:
        config = json.load(fp)
    return config


def get_topology_definition(topology_name=None):
    """Fetch a topology name and definition file.  If the topology_name is
    None, and there's only one topology definiton listed, we'll select that
    one, otherwise we'll die to avoid ambiguity.

    :param topology_name: a `str`, the topology_name of the topology (without
                          .py extension).
    :returns: a `tuple` containing (topology_name, topology_file).
    """
    config = get_config()
    topology_path = config["topology_specs"]
    if topology_name is None:
        topology_files = glob("{}/*.py".format(topology_path))
        if not topology_files:
            die("No topology definitions are defined in {}."
                .format(topology_path))
        if len(topology_files) > 1:
            die("Found more than one topology definition file in {specs_dir}. "
                "When more than one topology definition file exists, you must "
                "explicitly specify the topology by name using the -n or "
                "--name flags.".format(specs_dir=topology_path))
        topology_file = topology_files[0]
        topology_name = re.sub(r'(^{}|\.py$)'.format(topology_path), '',
                               topology_file)
    else:
        topology_file = "{}.py".format(os.path.join(topology_path,
                                                    topology_name))
        if not os.path.exists(topology_file):
            die("Topology definition file not found {}. You need to "
                "create a topology definition file first."
                .format(topology_file))

    return (topology_name, topology_file)


def get_env_config(env_name=None):
    """Fetch an environment name and config object from the config.json file.
    If the name is None and there's only one environment, we'll select the
    first, otherwise we'll die to avoid ambiguity.

    :returns: a `tuple` containing (env_name, env_config).
    """
    config = get_config()
    if env_name is None and len(config["envs"]) == 1:
        env_name = list(config["envs"].keys())[0]
    elif env_name is None and len(config["envs"]) > 1:
        die("Found more than one environment in config.json. "
            "When more than one environment exists, you must "
            "explicitly specify the environment name via the "
            "-e or --environment flags.")
    if env_name not in config["envs"]:
        die("Could not find a \"{}\" in config.json, have you specified one?"
            .format(env_name))

    return (env_name, config["envs"][env_name])


def get_nimbus_host_port(env_config):
    """Get the Nimbus server's hostname and port from a streamparse project's
    config file.

    :param env_config: The project's parsed config.
    :type env_config: `dict`

    :returns: (host, port)
    """
    if not env_config["nimbus"]:
        die("No Nimbus server configured in config.json.")

    if ":" in env_config["nimbus"]:
        host, port = env_config["nimbus"].split(":", 1)
        port = int(port)
    else:
        host = env_config["nimbus"]
        port = 6627
    return (host, port)


def get_nimbus_client(env_config=None, host=None, port=None, timeout=7000):
    """Get a Thrift RPC client for Nimbus given project's config file.

    :param env_config: The project's parsed config.
    :type env_config: `dict`
    :param host: The host to use for Nimbus.  If specified, `env_config` will
                 not be consulted.
    :type host: `str`
    :param port: The port to use for Nimbus.  If specified, `env_config` will
                 not be consulted.
    :type port: `int`
    :param timeout: The time to wait (in milliseconds) for a response from
                    Nimbus.
    :param timeout: `int`

    :returns: a ThriftPy RPC client to use to communicate with Nimbus
    """
    if host is None:
        host, port = get_nimbus_host_port(env_config)
    nimbus_client = make_client(storm_thrift.Nimbus, host=host, port=port,
                                proto_factory=TBinaryProtocolFactory(),
                                trans_factory=TFramedTransportFactory(),
                                timeout=timeout)
    return nimbus_client


def is_ssh_for_nimbus(env_config):
    """Check if we need to use SSH access to Nimbus or not.
    """
    return env_config.get('use_ssh_for_nimbus', True)


def storm_lib_version():
    """Get the Storm library version being used by Leiningen and Streamparse.

    :returns: The Storm library version specified in project.clj
    :rtype: pkg_resources.Version
    """
    deps_tree = run("lein deps :tree", hide=True).stdout
    pattern = r'\[org\.apache\.storm/storm-core "([^"]+)"\]'
    versions = set(re.findall(pattern, deps_tree))

    if len(versions) > 1:
        raise RuntimeError("Multiple Storm Versions Detected.")
    elif len(versions) == 0:
        raise RuntimeError("No Storm version specified in project.clj "
                           "dependencies.")
    else:
        return parse_version(versions.pop())


def get_ui_jsons(env_name, api_paths):
    """Take env_name as a string and api_paths that should
    be a list of strings like '/api/v1/topology/summary'
    """
    _, env_config = get_env_config(env_name)
    host, _ = get_nimbus_host_port(env_config)
    # TODO: Get remote_ui_port from storm?
    remote_ui_port = 8080
    # SSH tunnel can take a while to close. Check multiples if necessary.
    local_ports = list(range(8081, 8090))
    shuffle(local_ports)
    for local_port in local_ports:
        try:
            data = {}
            with ssh_tunnel(env_config.get("user"), host, local_port=local_port,
                            remote_port=remote_ui_port):
                for api_path in api_paths:
                    r = requests.get('http://127.0.0.1:%s%s' % (local_port,
                                                                api_path))
                    data[api_path] = r.json()
            return data
        except Exception as e:
            if "already in use" in str(e):
                continue
            raise
    raise RuntimeError("Cannot find local port for SSH tunnel to Storm Head.")


def get_ui_json(env_name, api_path):
    """Take env_name as a string and api_path that should
    be a string like '/api/v1/topology/summary'
    """
    return get_ui_jsons(env_name, [api_path])[api_path]


def prepare_topology():
    """Prepare a topology for running locally or deployment to a remote
    cluster.
    """
    resources_dir = join("_resources", "resources")
    if os.path.isdir(resources_dir):
        shutil.rmtree(resources_dir)
    shutil.copytree("src", resources_dir)


def _get_file_names_command(path, patterns):
    """Given a list of bash `find` patterns, return a string for the
    bash command that will find those pystorm log files
    """
    patterns = "' -o -name '".join(patterns)
    return ("cd {path} && "
            "find . -maxdepth 1 -name '{patterns}' ") \
            .format(path=path, patterns=patterns)


def get_logfiles_cmd(topology_name=None, pattern=None):
    """ Returns a string representing a command to run on the Storm workers that
    will yield all of the logfiles for the given topology that meet the given
    pattern (if specified).
    """
    # list log files found
    log_name_patterns = ["worker*", "supervisor*", "access*", "metrics*",
                         "streamparse_{topo_name}*".format(topo_name=topology_name)]
    ls_cmd = _get_file_names_command(env.log_path, log_name_patterns)
    if pattern is not None:
        ls_cmd += " | egrep '{pattern}'".format(pattern=pattern)
    return ls_cmd


def print_stats_table(header, data, columns, default_alignment='l',
                      custom_alignment=None):
    """Print out a list of dictionaries (or objects) as a table.

    If given a list of objects, will print out the contents of objects'
    `__dict__` attributes.

    :param header: Header that will be printed above table.
    :type header:  `str`
    :param data:   List of dictionaries (or objects )
    """
    print("# %s" % header)
    table = PrettyTable(columns)
    table.align = default_alignment
    if not isinstance(data, list):
        data = [data]
    for row in data:
        # Treat all objects like dicts to make life easier
        if not isinstance(row, dict):
            row = row.__dict__
        table.add_row([row.get(key, "MISSING") for key in columns])
    if custom_alignment:
        for column, alignment in iteritems(custom_alignment):
            table.align[column] = alignment
    print(table)

