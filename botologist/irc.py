import logging
log = logging.getLogger(__name__)

import signal
import socket
import threading

import botologist.util


class User:
	def __init__(self, nick, host=None, ident=None):
		self.nick = nick
		if host and '@' in host:
			host = host[host.index('@')+1:]
		self.host = host
		if ident and ident[0] == '~':
			ident = ident[1:]
		self.ident = ident

	@classmethod
	def from_ircformat(cls, string):
		if string[0] == ':':
			string = string[1:]
		parts = string.split('!')
		nick = parts[0]
		ident, host = parts[1].split('@')
		return cls(nick, host, ident)

	def __eq__(self, other):
		if not isinstance(other, self.__class__):
			return False
		return other.host == self.host


class Message:
	def __init__(self, source, target, message=None):
		self.user = User.from_ircformat(source)
		self.target = target
		self.message = message
		self.words = message.strip().split()
		self.channel = None

	@classmethod
	def from_privmsg(cls, msg):
		words = msg.split()
		return cls(words[0][1:], words[2], ' '.join(words[3:])[1:])

	@property
	def is_private(self):
		return self.target[0] != '#'


class Server:
	def __init__(self, address):
		parts = address.split(':')
		self.host = parts[0]
		if len(parts) > 1:
			self.port = int(parts[1])
		else:
			self.port = 6667
		self.channels = {}

	def add_channel(self, channel):
		assert isinstance(channel, Channel)
		self.channels[channel.channel] = channel


class Channel:
	def __init__(self, channel):
		if channel[0] != '#':
			channel = '#' + channel
		self.channel = channel
		self.host_map = {}
		self.nick_map = {}
		self.allow_colors = True

	def add_user(self, user):
		assert isinstance(user, User)
		self.host_map[user.host] = user.nick
		self.nick_map[user.nick] = user.host

	def find_nick_from_host(self, host):
		if '@' in host:
			host = host[host.index('@')+1:]
		if host in self.host_map:
			return self.host_map[host]
		return False

	def find_host_from_nick(self, nick):
		if nick in self.nick_map:
			return self.nick_map[nick]
		return False

	def remove_user(self, nick=None, host=None):
		assert nick or host

		if host and '@' in host:
			host = host[host.index('@')+1:]

		if nick is not None and nick in self.nick_map:
			host = self.nick_map[nick]
		if host is not None and host in self.host_map:
			nick = self.host_map[host]
		if nick is not None and nick in self.nick_map:
			del self.nick_map[nick]
		if host is not None and host in self.host_map:
			del self.host_map[host]

	def update_nick(self, user, new_nick):
		assert isinstance(user, User)

		old_nick = user.nick
		if old_nick in self.nick_map:
			del self.nick_map[old_nick]

		self.nick_map[new_nick] = user.host
		self.host_map[user.host] = new_nick


class IRCSocketError(OSError):
	pass


class IRCSocket:
	def __init__(self, server):
		self.server = server
		self.socket = None

	def connect(self):
		addrinfo = socket.getaddrinfo(
			self.server.host, self.server.port,
			socket.AF_UNSPEC, socket.SOCK_STREAM
		)

		for res in addrinfo:
			af, socktype, proto, canonname, sa = res

			try:
				self.socket = socket.socket(af, socktype, proto)
			except OSError:
				self.socket = None
				continue

			try:
				self.socket.connect(sa)
			except OSError:
				self.socket.close()
				self.socket = None
				continue

			# if we reach this point, the socket has been successfully created,
			# so break out of the loop
			break

		if self.socket is None:
			raise IRCSocketError('Could not open socket')

	def recv(self, bufsize=4096):
		data = self.socket.recv(bufsize)

		# 13 = \r -- 10 = \n
		while data != b'' and (data[-1] != 10 and data[-2] != 13):
			data += self.socket.recv(bufsize)

		if data == b'':
			raise IRCSocketError('Received empty binary data')

		return botologist.util.decode(data)

	def send(self, data):
		if isinstance(data, str):
			data = data.encode('utf-8')
		self.socket.send(data)

	def close(self):
		self.socket.close()


class Connection:
	MAX_MSG_CHARS = 500

	def __init__(self, nick, username=None, realname=None):
		self.nick = nick
		self.username = username or nick
		self.realname = realname or nick
		self.irc_socket = None
		self.server = None
		self.channels = {}
		self.on_welcome = []
		self.on_join = []
		self.on_privmsg = []
		self.error_handler = None
		self.quitting = False
		self.reconnect_timer = False
		self.ping_timer = None
		self.ping_response_timer = None

	def connect(self, server):
		assert isinstance(server, Server)
		if self.irc_socket is not None:
			self.disconnect()
		self.server = server
		thread = threading.Thread(target=self._connect)
		thread.start()

	def disconnect(self):
		self.irc_socket.close()
		self.irc_socket = None

	def reconnect(self, time=None):
		if self.irc_socket:
			self.disconnect()

		if time:
			log.info('Reconnecting in %d seconds', time)
			thread = self.reconnect_timer = threading.Timer(time, self._connect)
		else:
			thread = threading.Thread(target=self._connect)

		thread.start()

	def _connect(self):
		if self.reconnect_timer:
			self.reconnect_timer = None

		log.info('Connecting to %s:%s', self.server.host, self.server.port)
		self.irc_socket = IRCSocket(self.server)
		self.irc_socket.connect()
		log.info('Successfully connected to server!')

		self.send('NICK ' + self.nick)
		self.send('USER ' + self.username + ' 0 * :' + self.realname)
		self.loop()

	def loop(self):
		while self.irc_socket:
			try:
				data = self.irc_socket.recv()
			except OSError:
				if self.quitting:
					log.info('socket.recv threw an exception, but the client '
						'is quitting, so exiting loop', exc_info=True)
				else:
					log.exception('socket.recv threw an exception')
					self.reconnect(5)
				return

			for msg in data.split('\r\n'):
				if not msg:
					continue

				log.debug('RECEIVED: %s', repr(msg))
				try:
					self.handle_msg(msg)
				except:
					# if an error handler is defined, call it and continue
					# the loop. if not, re-raise the exception
					if self.error_handler:
						self.error_handler()
					else:
						raise

	def join_channel(self, channel):
		assert isinstance(channel, Channel)
		log.info('Joining channel: %s', channel.channel)
		self.channels[channel.channel] = channel
		self.send('JOIN ' + channel.channel)

	def handle_msg(self, msg):
		words = msg.split()

		if words[0] == 'PING':
			self.reset_ping_timer()
			self.send('PONG ' + words[1])
		elif words[0] == 'PONG':
			self.reset_ping_timer()
		elif words[0] == 'ERROR':
			if ':Your host is trying to (re)connect too fast -- throttled' in msg:
				log.warning('Throttled for (re)connecting too fast')
				self.reconnect(60)
			else:
				log.warning('Received error: %s', msg)
				self.reconnect(10)
		elif words[0] > '400' and words[0] < '600':
			log.warning('Received error reply: %s', msg)
		elif len(words) > 1:
			if words[1] == '001':
				# welcome message, lets us know that we're connected
				for callback in self.on_welcome:
					callback()

			elif words[1] == 'JOIN':
				user = User.from_ircformat(words[0])
				channel = words[2]
				log.debug('User %s (%s @ %s) joined channel %s',
					user.nick, user.ident, user.host, channel)
				if user.nick == self.nick:
					self.send('WHO '+channel)
				else:
					self.channels[words[2]].add_user(user)
					for callback in self.on_join:
						callback(self.channels[words[2]], user)

			# response to WHO command
			elif words[1] == '352':
				channel = words[3]
				ident = words[4]
				host = words[5]
				nick = words[7]
				user = User(nick, host, ident)
				self.channels[channel].add_user(user)

			elif words[1] == 'NICK':
				user = User.from_ircformat(words[0])
				new_nick = words[2][1:]
				log.debug('User %s changing nick: %s', user.host, new_nick)
				for channel in self.channels.values():
					if channel.find_nick_from_host(user.host):
						log.debug('Updating nick for user in channel %s',
							channel.channel)
						channel.update_nick(user, new_nick)

			elif words[1] == 'PART':
				user = User.from_ircformat(words[0])
				channel = words[2]
				self.channels[channel].remove_user(host=user.host)
				log.debug('User %s parted from channel %s', user.host, channel)

			elif words[1] == 'QUIT':
				user = User.from_ircformat(words[0])
				log.debug('User %s quit', user.host)
				for channel in self.channels.values():
					if channel.find_nick_from_host(user.host):
						channel.remove_user(host=user.host)
						log.debug('Removing user from channel %s', channel.channel)

			elif words[1] == 'PRIVMSG':
				message = Message.from_privmsg(msg)
				if not message.is_private:
					message.channel = self.channels[message.target]
					if message.user.host not in self.channels[message.target].host_map:
						log.debug('Unknown user %s (%s) added to channel %s',
							message.user.nick, message.user.host, message.target)
						self.channels[message.target].add_user(message.user)
				for callback in self.on_privmsg:
					callback(message)

	def send_msg(self, target, message):
		if target in self.channels:
			if not self.channels[target].allow_colors:
				message = botologist.util.strip_irc_formatting(message)
		if not isinstance(message, list):
			message = message.split('\n')
		for privmsg in message:
			self.send('PRIVMSG ' + target + ' :' + privmsg)

	def send(self, msg):
		if len(msg) > self.MAX_MSG_CHARS:
			log.warning('Message too long (%d characters), upper limit %d',
				len(msg), self.MAX_MSG_CHARS)
			msg = msg[:(self.MAX_MSG_CHARS - 3)] + '...'

		log.debug('SENDING: %s', repr(msg))
		self.irc_socket.send(msg + '\r\n')

	def quit(self, reason='Leaving'):
		if self.reconnect_timer:
			log.info('Aborting reconnect timer')
			self.reconnect_timer.cancel()
			self.reconnect_timer = None
			return

		if not self.irc_socket:
			log.warning('Tried to quit, but irc_socket is None')
			return

		log.info('Quitting, reason: '+reason)
		self.quitting = True
		self.send('QUIT :' + reason)

	def reset_ping_timer(self):
		if self.ping_response_timer:
			self.ping_response_timer.cancel()
			self.ping_response_timer = None
		if self.ping_timer:
			self.ping_timer.cancel()
			self.ping_timer = None
		self.ping_timer = threading.Timer(5*60, self.send_ping)
		self.ping_timer.start()

	def send_ping(self):
		if self.ping_response_timer:
			log.warning('Already waiting for PONG, cannot send another PING')
			return

		self.send('PING ' + self.server.host)
		self.ping_response_timer = threading.Timer(10, self.handle_ping_timeout)
		self.ping_response_timer.start()

	def handle_ping_timeout(self):
		log.warning('Ping timeout')
		self.ping_response_timer = None
		self.reconnect()


class Client:
	def __init__(self, server, nick='__bot__', username=None, realname=None):
		self.conn = Connection(nick, username, realname)
		self.server = Server(server)
		self.conn.on_welcome.append(self._join_channels)

	@property
	def nick(self):
		return self.conn.nick

	def add_channel(self, channel):
		channel = Channel(channel)
		self.server.add_channel(channel)

	def _join_channels(self):
		for channel in self.server.channels.values():
			self.conn.join_channel(channel)

	def run_forever(self):
		log.info('Starting client!')

		def sigterm_handler(signo, stack_frame): # pylint: disable=unused-argument
			self.stop('Terminating, probably back soon!')
		signal.signal(signal.SIGQUIT, sigterm_handler)
		signal.signal(signal.SIGTERM, sigterm_handler)
		signal.signal(signal.SIGINT, sigterm_handler)

		try:
			self.conn.connect(self.server)
		except (InterruptedError, SystemExit, KeyboardInterrupt):
			self.stop('Terminating, probably back soon!')
		except:
			self.stop('An error occured!')
			raise

	def stop(self, msg='Leaving'):
		self.conn.quit(msg)
