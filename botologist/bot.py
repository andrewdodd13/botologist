import logging
log = logging.getLogger(__name__)

import datetime
import threading
import importlib

import botologist.error
import botologist.http
import botologist.irc
import botologist.plugin
import botologist.util


class CommandMessage:
	"""Representation of an IRC message that is a command.

	When a user sends a message to the bot that is a bot command, an instance
	of this class should be constructed and will be passed to the command
	handler to figure out a response.
	"""
	def __init__(self, message):
		assert isinstance(message, botologist.irc.Message)
		self.message = message
		self.command = message.words[0]
		self.args = message.words[1:]

	@property
	def user(self):
		return self.message.user

	@property
	def target(self):
		return self.message.target


class Channel:
	"""Channel proxy class.

	Added functionality for adding various handlers from plugins, as plugins are
	registered on a per-channel basis.
	"""
	def __init__(self, channel):
		assert isinstance(channel, botologist.irc.Channel)
		self._channel = channel
		self.commands = {}
		self.joins = []
		self.replies = []
		self.tickers = []
		self.admins = []
		self.http_handlers = []
		self.plugins = []

	@property
	def channel(self):
		return self._channel.channel

	@property
	def host_map(self):
		return self._channel.host_map

	@property
	def nick_map(self):
		return self._channel.nick_map

	@property
	def allow_colors(self):
		return self._channel.allow_colors

	@allow_colors.setter
	def allow_colors(self, value):
		self._channel.allow_colors = value

	def add_user(self, user):
		return self._channel.add_user(user)

	def find_nick_from_host(self, host):
		return self._channel.find_nick_from_host(host)

	def find_host_from_nick(self, nick):
		return self._channel.find_host_from_nick(nick)

	def remove_user(self, nick=None, host=None):
		return self._channel.remove_user(nick=nick, host=host)

	def update_nick(self, user, new_nick):
		return self._channel.update_nick(user, new_nick)

	def register_plugin(self, plugin):
		assert isinstance(plugin, botologist.plugin.Plugin)
		self.plugins.append(plugin.__class__.__name__)
		for cmd, callback in plugin.commands.items():
			self.commands[cmd] = callback
		for join_callback in plugin.joins:
			self.joins.append(join_callback)
		for reply_callback in plugin.replies:
			self.replies.append(reply_callback)
		for tick_callback in plugin.tickers:
			self.tickers.append(tick_callback)
		for http_handler in plugin.http_handlers:
			self.http_handlers.append(http_handler)


class Bot():
	"""IRC bot."""

	version = None

	# the character commands start with
	CMD_PREFIX = '!'

	# ticker interval in seconds
	TICK_INTERVAL = 120

	# spam throttling in seconds
	SPAM_THROTTLE = 2

	def __init__(self, config):
		bot_config = config.get('bot', {})

		# some config options will be moved from "bot" to the root of the config
		def get_config_compat(key, default=None):
			if key in bot_config:
				return bot_config.get(key)
			return config.get(key, default)

		nick = get_config_compat('nick', 'botologist')
		self.client = botologist.irc.Client(
			server=get_config_compat('server'),
			nick=nick,
			username=get_config_compat('username', nick),
			realname=get_config_compat('realname', nick),
		)

		self.config = config
		self.storage_dir = config['storage_dir']

		self.admins = get_config_compat('admins', [])
		self.bans = get_config_compat('bans', [])
		self.global_plugins = config.get('global_plugins', [])

		self.plugins = {}
		self._command_log = {}
		self._last_command = None
		self._reply_log = {}
		self.timer = None

		self.http_port = get_config_compat('http_port')
		self.http_host = get_config_compat('http_host')
		self.http_server = None

		self.error_handler = botologist.error.ErrorHandler(self)
		self.conn.error_handler = self.error_handler.handle_error
		self.conn.on_welcome.append(self._start_tick_timer)
		self.conn.on_join.append(self._handle_join)
		self.conn.on_privmsg.append(self._handle_privmsg)

		# configure plugins
		for name, plugin_class in config.get('plugins', {}).items():
			# convenience compatibility layer for when plugins module was moved
			plugin_class = plugin_class.replace('botologist.plugin.', 'plugins.')

			# dynamically import the plugin module and pass the class
			parts = plugin_class.split('.')
			module = importlib.import_module('.'.join(parts[:-1]))
			plugin_class = getattr(module, parts[-1])
			self.register_plugin(name, plugin_class)

		# configure channels
		channels = config.get('channels')
		if isinstance(channels, dict):
			for name, channel in channels.items():
				self.add_channel(name, **channel)
		elif isinstance(channels, list):
			for channel in channels:
				if isinstance(channel, dict):
					name = channel.pop('channel')
				else:
					name = channel
					channel = {}
				self.add_channel(name, **channel)

	@property
	def conn(self):
		return self.client.conn

	@property
	def nick(self):
		return self.client.nick

	@property
	def server(self):
		return self.client.server

	@property
	def channels(self):
		return self.server.channels

	def get_admin_nicks(self):
		admin_nicks = set()
		for channel in self.server.channels.values():
			for admin_host in self.admins:
				nick = channel.find_nick_from_host(admin_host)
				if nick:
					admin_nicks.add(nick)
		return admin_nicks

	def run_forever(self):
		if self.http_port:
			log.info('Running HTTP server on %s:%s', self.http_host, self.http_port)
			thread = botologist.util.ErrorProneThread(
				target=botologist.http.run_http_server,
				args=(self, self.http_host, self.http_port),
				error_handler=self.error_handler.handle_error)
			thread.start()
		super().run_forever()

	def stop(self, msg=None):
		if self.http_server:
			log.info('Shutting down HTTP server')
			self.http_server.shutdown()
			self.http_server = None

		if self.timer:
			log.info('Ticker stopped')
			self.timer.cancel()
			self.timer = None

		super().stop(msg)

	def register_plugin(self, name, plugin):
		if isinstance(plugin, str):
			parts = plugin.split('.')
			try:
				module = importlib.import_module('.'.join(parts[:-1]))
				plugin = getattr(module, parts[-1])
			except (AttributeError, ImportError) as exception:
				msg = 'Could not find plugin class: {}'.format(plugin)
				raise Exception(msg) from exception

		assert issubclass(plugin, botologist.plugin.Plugin)
		log.debug('Plugin "%s" registered', name)
		self.plugins[name] = plugin

	def add_channel(self, channel, plugins=None, admins=None, allow_colors=True):
		def guess_plugin_class(plugin):
			plugin_class = ''.join(part.title() for part in plugin.split('_'))
			return 'plugins.{}.{}Plugin'.format(plugin, plugin_class)

		if not isinstance(channel, Channel):
			channel = Channel(channel)

		# channel-specific plugins
		if plugins:
			assert isinstance(plugins, list)
			for plugin in plugins:
				assert isinstance(plugin, str)
				if not plugin in self.plugins:
					plugin_class = guess_plugin_class(plugin)
					self.register_plugin(plugin, plugin_class)
				log.debug('Adding plugin %s to channel %s', plugin, channel.channel)
				channel.register_plugin(self.plugins[plugin](self, channel))

		# global plugins
		for plugin in self.global_plugins:
			assert isinstance(plugin, str)
			if not plugin in self.plugins:
				plugin_class = guess_plugin_class(plugin)
				self.register_plugin(plugin, plugin_class)
			log.debug('Adding plugin %s to channel %s', plugin, channel.channel)
			channel.register_plugin(self.plugins[plugin](self, channel))

		if admins:
			assert isinstance(admins, list)
			channel.admins = admins

		channel.allow_colors = allow_colors

		self.server.channels[channel.channel] = channel

	def _send_msg(self, msgs, targets):
		if targets == '*':
			targets = (channel for channel in self.server.channels)
		elif not isinstance(targets, list) and not isinstance(targets, set):
			targets = set([targets])

		if not isinstance(msgs, list) and not isinstance(msgs, set):
			msgs = set([msgs])

		for msg in msgs:
			for target in targets:
				self.conn.send_msg(target, msg)

	def _handle_join(self, channel, user):
		assert isinstance(channel, Channel)
		assert isinstance(user, botologist.irc.User)

		# iterate through join callbacks. the first, if any, to return a
		# non-empty value, will be sent back to the channel as a response.
		response = None
		for join_func in channel.joins:
			response = join_func(user, channel)
			if response:
				self._send_msg(response, channel.channel)
				return

	def _handle_privmsg(self, message):
		assert isinstance(message, botologist.irc.Message)

		if message.user.host in self.bans:
			return

		# self-explanatory...
		if message.is_private:
			log.debug('Message is private, not replying')
			return None

		# check if the user is an admin - add it to the message.user object for
		# later re-use
		message.user.is_admin = (message.user.host in self.admins or
			message.user.host in message.channel.admins)

		channel = self.conn.channels[message.target]
		assert isinstance(channel, Channel)

		if message.message.startswith(self.CMD_PREFIX):
			return self._handle_command(message, channel)

		# otherwise, call the channel's repliers
		response = self._call_repliers(channel, message)

		if response:
			self._send_msg(response, message.target)

	def _handle_command(self, message, channel):
		# if the message starts with the command prefix, check for mathing
		# command and fire its callback
		cmd_string = message.words[0][1:].lower()

		if cmd_string in channel.commands:
			command = CommandMessage(message)
			command_func = channel.commands[cmd_string]
		else:
			matching_commands = [cmd for cmd in channel.commands
				if cmd.startswith(cmd_string)]
			if len(matching_commands) == 0:
				log.debug('"%s" did not match any commands in channel %s',
					cmd_string, channel.channel)
				return
			elif len(matching_commands) != 1:
				log.debug('"%s" matched more than 1 command in channel %s',
					cmd_string, channel.channel)
				return

			command = CommandMessage(message)
			command.command = self.CMD_PREFIX + matching_commands[0]
			command_func = channel.commands[matching_commands[0]]

		if command_func._is_threaded:
			log.debug('Starting thread for command %s', cmd_string)
			thread = botologist.util.ErrorProneThread(
				target=self._maybe_send_cmd_reply,
				args=(command_func, command),
				error_handler=self.error_handler.handle_error)
			thread.start()
		else:
			self._maybe_send_cmd_reply(command_func, command)

	def _maybe_send_cmd_reply(self, command_func, message):
		response = self._call_command(command_func, message)
		if response:
			self._send_msg(response, message.target)

	def _call_command(self, command_func, command):
		# check for spam
		now = datetime.datetime.now()
		if command.command in self._command_log and not command.user.is_admin:
			diff = now - self._command_log[command.command]
			if self._last_command == (command.user.host, command.command, command.args):
				threshold = self.SPAM_THROTTLE * 3
			else:
				threshold = self.SPAM_THROTTLE
			if diff.seconds < threshold:
				log.info('Command throttled: %s', command.command)
				return None

		# log the command call for spam throttling
		self._last_command = (command.user.host, command.command, command.args)
		self._command_log[command.command] = now

		return command_func(command)

	def _call_repliers(self, channel, message):
		now = datetime.datetime.now()
		final_replies = []

		# iterate through reply callbacks
		for reply_func in channel.replies:
			replies = reply_func(message)

			if not replies:
				continue

			if isinstance(replies, list):
				final_replies = final_replies + replies
			else:
				final_replies.append(replies)

		if not message.user.is_admin:
			for reply in final_replies:
				# throttle spam - prevents the same reply from being sent
				# more than once in a row within the throttle threshold
				if channel.channel not in self._reply_log:
					self._reply_log[channel.channel] = {}

				if reply in self._reply_log[channel.channel]:
					diff = now - self._reply_log[channel.channel][reply]
					if diff.seconds < self.SPAM_THROTTLE:
						log.info('Reply throttled: "%s"', reply)
						final_replies.remove(reply)

				# log the reply for spam throttling
				self._reply_log[channel.channel][reply] = now

		return final_replies

	def _start_tick_timer(self):
		self.timer = threading.Timer(self.TICK_INTERVAL, self._tick)
		self.timer.start()
		log.debug('Ticker started')

	def _tick(self):
		log.debug('Tick!')

		# reset the spam throttle to prevent the log dictionaries from becoming
		# too large
		self._command_log = {}
		for channel in self._reply_log:
			self._reply_log[channel] = {}

		try:
			for channel in self.server.channels.values():
				for ticker in channel.tickers:
					result = ticker()
					if result:
						self._send_msg(result, channel.channel)
		finally:
			self._start_tick_timer()
