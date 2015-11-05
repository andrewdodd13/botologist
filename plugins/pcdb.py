import requests

import botologist.plugin


class PCDB:
	@classmethod
	def get_random(cls):
		if not cls.comments:
			print('requesting')
			response = requests.get('http://pcdb.lutro.me',
				headers={'accept': 'application/json'})
			cls.comments = [c['body'] for c in response.json()['comments']]
		return cls.comments.pop()
PCDB.comments = []


class PcdbPlugin(botologist.plugin.Plugin):
	"""porn comments database plugin."""

	@botologist.plugin.command('pcdb', alias='random')
	def get_pcdb_random(self, cmd):
		return PCDB.get_random()
