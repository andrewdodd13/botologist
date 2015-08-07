import unittest.mock as mock
from tests.plugin import PluginTestCase

class UrlPluginTest(PluginTestCase):
	def create_plugin(self):
		from ircbot.plugin.urls import UrlPlugin
		return UrlPlugin(self.bot, self.channel)

	def test_unshortens_url(self):
		url = 'http://www.foobar.com'
		with mock.patch('ircbot.plugin.urls.get_location', return_value=url):
			ret = self.reply('asdad http://t.co/asdf sfgdgf')
		self.assertEqual(['http://t.co/asdf => http://www.foobar.com'], ret)
