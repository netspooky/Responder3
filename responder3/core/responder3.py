import os
import sys
import copy
import json
import logging
import argparse
import datetime
import asyncio
import itertools
import multiprocessing
import importlib.machinery
import importlib.util
import warnings

from responder3.core.commons import handle_systemd, defaultports, tracefunc
from responder3.core.interfaceutil import interfaces
from responder3.core.logtask import LogProcessor, LogEntry
from responder3.core.servertask import Responder3ServerTask


class ServerTaskEntry:
	def __init__(self, taskid):
		self.taskid = taskid
		self.startup_config = None
		self.task = None
		self.command_channel_manager, self.command_channel_client = multiprocessing.Pipe()
		self.created_at = datetime.datetime.utcnow()
		self.started_at = None


class Responder3:
	def __init__(self):
		self.loop = asyncio.get_event_loop()
		self.config = None

		self.reverse_domain_table = {}

		self.log_queue = asyncio.Queue()
		self.log_command_queue = asyncio.Queue()
		self.logprocessor = None
		self.test_output_queue = None

		self.override_interfaces = None
		self.override_ipv4 = None
		self.override_ipv6 = None
		self.override_verb = None

		self.server_task_id = 0
		self.servers = []
		self.server_tasks = {}
		self.reverse_domain_table = None


	@staticmethod
	def get_argparser():
		parser = argparse.ArgumentParser(
			description='Responder3',
			epilog='List of available interfaces:\r\n' + str(interfaces),
			formatter_class=argparse.RawTextHelpFormatter
		)
		parser.add_argument(
			"-c",
			"--config",
			help="Configuration file (JSON). Full path please"
		)
		parser.add_argument(
			"-p",
			"--python-config",
			help="Configuration file (Python). Full path please"
		)
		parser.add_argument(
			"-e",
			"--environ-config",
			action='store_true',
			help="Configuration file is set via OS environment variable (Python script)"
		)
		parser.add_argument(
			"-I",
			action='append',
			help="Interface to bind to, can be multiple by providing sequential -I. Overrides bind_iface parameter in configs."
		)
		parser.add_argument(
			"-4",
			action='store_true',
			dest='ip4',
			help="IP version 4 to be used. Overrides bind_family in config settings."
		)
		parser.add_argument(
			"-6",
			action='store_true',
			dest='ip6',
			help="IP version 6 to be used. Overrides bind_family in config settings."
		)
		parser.add_argument(
			"-L",
			action='store_true',
			dest='list_interfaces',
			help="List all interfaces with assigned IPv4 and IPv6 addresses then exit."
		)
		parser.add_argument(
			'-v',
			'--verbose',
			action='count',
			default=0
		)
		return parser

	@staticmethod
	def from_args(args):
		responder = Responder3()
		responder.override_interfaces = args.I
		responder.override_ipv4 = args.ip4
		responder.override_ipv6 = args.ip6
		responder.override_verb = args.verbose
		if args.config is not None:
			print(args.config)
			responder.config = Responder3Config.from_file(args.config)
		elif args.python_config is not None:
			responder.config = Responder3Config.from_python_script(args.python_config)
		elif args.environ_config is not None:
			responder.config = Responder3Config.from_os_env()
		else:
			raise Exception(
				'No suitable configuration method was supplied!'
				'Use either -e or -c or -p'
			)
		return responder

	@staticmethod
	def from_config(config, override_interfaces = None, override_ipv4 = None, override_ipv6=None, override_verb=None, output_queue = None):
		responder = Responder3()
		responder.override_interfaces = override_interfaces
		responder.override_ipv4 = override_ipv4
		responder.override_ipv6 = override_ipv6
		responder.override_verb = override_verb
		responder.test_output_queue = output_queue
		responder.config = config
		return responder

	async def aio_log(self, logentry):
		await self.log_queue.put(logentry)

	def log(self, message, level=logging.INFO):
		log = LogEntry(level, 'Responder3 MAIN', message)
		self.loop.create_task(self.aio_log(log))

	def get_taskid(self):
		taskid = self.server_task_id
		self.server_task_id += 1
		return taskid

	def start_server_task(self, serverconfig):
		ste = ServerTaskEntry(self.get_taskid())
		temp = copy.deepcopy(serverconfig)
		ste.startup_config = temp
		ste.task = Responder3ServerTask(
			log_queue = self.log_queue,
			reverse_domain_table=self.reverse_domain_table,
			server_command_queue=None,
			loop=self.loop
		)
		coro = ste.task.create_server_coro(temp)
		self.server_tasks[ste.taskid] = ste
		self.loop.create_task(coro)
		ste.started_at = datetime.datetime.utcnow()
		del temp

	def get_server_process(self, taskid):
		if taskid not in self.server_tasks:
			return None
		return self.server_tasks[taskid]

	def get_taskid_list(self):
		return self.server_tasks.keys()

	def send_server_command(self, taskid, command):
		spe = self.get_server_process(taskid)
		if spe is None:
			return None
		spe.command_queue.put(command)

	def get_serverconfigs(self):
		# Setting up and starting servers
		for serverentry in self.config.server_settings:
			if self.override_interfaces is None:
				ifaces = serverentry.get('bind_iface', None)
				if ifaces is None:
					raise Exception('Interface name MUST be provided!')
				if not isinstance(ifaces, list):
					ifaces = [ifaces]

			else:
				ifaces = self.override_interfaces

			bind_family = []
			if self.override_ipv4:
				bind_family.append(4)
			if self.override_ipv6:
				bind_family.append(6)

			if bind_family == []:
				bind_family_conf = serverentry.get('bind_family', None)
				if bind_family_conf is not None:
					if not isinstance(bind_family_conf, list):
						bind_family.append(int(bind_family_conf))
					else:
						for ver in bind_family_conf:
							bind_family.append(int(ver))

			if bind_family == []:
				raise Exception('IP version (bind_family) MUST be set either in cofig file or in command line!')

			portspecs = serverentry.get(
				'bind_port',
				defaultports[serverentry['handler']] if serverentry['handler'] in defaultports else None
			)

			if portspecs is None:
				raise Exception('For protocol %s the port must be supplied!' % (serverentry['handler'],))

			if not isinstance(portspecs, list):
				portspecs = [portspecs]

			for element in itertools.product(ifaces, portspecs):
				socket_configs = interfaces.get_socketconfig(
					element[0], element[1][0], element[1][1],
					ipversion=bind_family
				)
				for socket_config in socket_configs:
					serverentry['listener_socket_config'] = socket_config
					yield serverentry

	def run(self):
		try:
			if self.config.startup is not None:
				if 'mode' in self.config.startup:
					if self.config.startup['mode'] == 'STANDARD':
						# starting in standalone mode...
						pass
					elif self.config.startup['mode'] == 'DEV':
						os.environ['PYTHONASYNCIODEBUG'] = '1'
						os.environ['R3DEEPDEBUG'] = '1'
						self.loop.set_debug(True)

						# Make the threshold for "slow" tasks very very small for
						# illustration. The default is 0.1, or 100 milliseconds.
						self.loop.slow_callback_duration = 0.001

						# Report all mistakes managing asynchronous resources.
						warnings.simplefilter('always', ResourceWarning)

						# sys.settrace(tracefunc)

					elif self.config.startup['mode'] == 'TEST':
						os.environ['PYTHONASYNCIODEBUG'] = '1'
						os.environ['R3DEEPDEBUG'] = '1'
						if self.test_output_queue is None:
							self.test_output_queue = multiprocessing.Queue()
						if 'extensions' not in self.config.log_settings:
							self.config.log_settings['handlers'] = {}
						if 'TEST' not in self.config.log_settings['handlers']:
							self.config.log_settings['handlers']['TEST'] = 'TEST'
						if 'TEST' not in self.config.log_settings:
							self.config.log_settings['TEST'] = {}
						if 'output_queue' not in self.config.log_settings['TEST']:
							self.config.log_settings['TEST']['output_queue'] = self.test_output_queue

					elif self.config.startup['mode'] == 'SERVICE':
						if 'pidfile' not in self.config.startup['settings']:
							raise Exception('pidfile MUST be set when running in service mode')
						handle_systemd(self.config.startup['settings']['pidfile'])

				else:
					# starting in standalone mode...
					pass
			else:
				# starting in standalone mode...
				pass

			self.logprocessor = LogProcessor(self.config.log_settings, self.log_queue)
			self.loop.create_task(self.logprocessor.run())

			for serverconfig in self.get_serverconfigs():
				self.start_server_task(serverconfig)

			self.log('Started all servers')
			self.loop.run_forever()

		except KeyboardInterrupt:
			self.log('CTRL+C pressed, exiting!')
			sys.exit(0)


class Responder3Config:
	CONFIG_OS_KEY = 'R3CONFIG'

	def __init__(self):
		self.startup = None
		self.log_settings = None
		self.server_settings = None

	@staticmethod
	def from_dict(config):
		conf = Responder3Config()
		conf.startup = config['startup']
		conf.log_settings = config['logsettings']
		conf.server_settings = config['servers']
		return conf

	@staticmethod
	def from_json(config_data):
		return Responder3Config.from_dict(json.loads(config_data))

	@staticmethod
	def from_file(file_path):
		with open(file_path, 'r') as f:
			config = json.load(f)
		return Responder3Config.from_dict(config)

	@staticmethod
	def from_python_script(file_path):
		loader = importlib.machinery.SourceFileLoader('responderconfig', file_path)
		spec = importlib.util.spec_from_loader(loader.name, loader)
		responderconfig = importlib.util.module_from_spec(spec)
		loader.exec_module(responderconfig)
		conf = Responder3Config()
		conf.startup = responderconfig.startup
		conf.log_settings = responderconfig.logsettings
		conf.server_settings = responderconfig.servers

		return conf

	@staticmethod
	def from_os_env():
		config_file = os.environ.get(Responder3Config.CONFIG_OS_KEY)
		if config_file is None:
			raise Exception(
				'Could not find configuration file path in os environment variables!'
				'Name to be set: %s' % Responder3Config.CONFIG_OS_KEY
			)
		return Responder3Config.from_python_script(config_file)
