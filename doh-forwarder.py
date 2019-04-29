#!/usr/bin/env python3
import asyncio
import aiohttp
import argparse
import logging
import struct
import random
import time


def main():
	# Attempt to use uvloop if installed for extra performance
	try:
		import uvloop
		asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
	except ImportError:
		pass

	# Handle command line arguments
	parser = argparse.ArgumentParser()
	parser.add_argument('-l', '--listen-address', nargs='+', default=['127.0.0.1', '::1'],
						help='addresses to listen on for DNS over HTTPS requests (default: %(default)s)')
	parser.add_argument('-p', '--listen-port', nargs='+', type=int, default=[53],
						help='ports to listen on for DNS over HTTPS requests (default: %(default)s)')
	parser.add_argument('-u', '--upstreams', nargs='+', default=['https://1.1.1.1:443/dns-query', 'https://1.0.0.1:443/dns-query'],
						help='upstream servers to forward DNS queries and requests to (default: %(default)s)')
	parser.add_argument('-t', '--tcp', action='store_true', default=False,
						help='serve TCP based queries and requests along with UDP (default: %(default)s)')
	args = parser.parse_args()

	headers = \
	{
		'accept': 'application/dns-message',
		'content-type': 'application/dns-message'
	}

	# Setup logging
	logging.basicConfig(level='INFO', format='[%(levelname)s] %(message)s')
	logging.info('Starting DNS over HTTPS forwarder')
	logging.info('Args: %r' % (vars(args)))

	# Setup event loop
	loop = asyncio.get_event_loop()

	# Setup DNS resolver to cache/forward queries and answers
	resolver = DohResolver([UpstreamContext(u, headers) for u in args.upstreams])

	# Setup listening transports
	transports = []
	for addr in args.listen_address:
		for port in args.listen_port:
			# Setup UDP server
			logging.info('Starting UDP server listening on %s#%d' % (addr, port))
			udp_listen = loop.create_datagram_endpoint(lambda: UdpDohProtocol(resolver), local_addr=(addr, port))
			udp, _ = loop.run_until_complete(udp_listen)
			transports.append(udp)

			# Setup TCP server
			if args.tcp:
				logging.info('Starting TCP server listening on %s#%d' % (addr, port))
				tcp_listen = loop.create_server(lambda: TcpDohProtocol(resolver), addr, port)
				tcp = loop.run_until_complete(tcp_listen)
				transports.append(tcp)

	# Serve forever
	try:
		loop.run_forever()
	except (KeyboardInterrupt, SystemExit):
		logging.info('Shutting down DNS over HTTPS forwarder')
		loop.run_until_complete(loop.shutdown_asyncgens())

	# Close upstream connections
	logging.info('Closing upstream connections')
	loop.run_until_complete(resolver.close())

	# Close listening servers
	logging.info('Closing listening transports')
	for transport in transports:
		transport.close()

	# Wait for operations to end and close event loop
	loop.run_until_complete(asyncio.sleep(0.3))
	loop.close()

class UpstreamContext:
	"""
	An object used to manage upstream server connections and metadata.
	"""

	def __init__(self, url, headers=None):
		self.url = url
		self.rtt = 0.0
		self.queries = 0
		self.answers = 0
		self.session = aiohttp.ClientSession(headers=headers)

	def get_stats(self):
		"""
		Returns a formatted string of statistics for this upstream server.
		"""

		return '%s (rtt: %.3f s, queries: %u, answers: %u)' % (self.url, self.rtt, self.queries, self.answers)


class DohResolver:
	"""
	An object used to manager upstream server contexts and resolve DNS over HTTPS queries.
	"""

	def __init__(self, upstreams):
		self._upstreams = upstreams
		self._queries = 0
		self._answers = 0

	def _select_upstream_rtt(self):
		"""
		Select a upstream server to forward to (biases towards upstreams with lower rtt).

		Returns:
				The selected upstream server.
		"""

		max_rtt = max([upstream.rtt for upstream in self._upstreams])
		return random.choices(self._upstreams, [max_rtt - upstream.rtt + 1 for upstream in self._upstreams])[0]

	def _select_upstream_random(self):
		"""
		Select a upstream server to forward to (random even distribution).

		Returns:
				The selected upstream server.
		"""

		return self._upstreams[random.randint(0, len(self._upstreams) - 1)]

	def get_stats(self):
		"""
		Returns a formatted string of statistics for this resolver.
		"""

		avg_rtt = sum([u.rtt for u in self._upstreams]) / len(self._upstreams)
		return '%x (avg_rtt: %.3f s, total_queries: %u, total_answers: %u)' % (id(self), avg_rtt, self._queries, self._answers)

	async def resolve(self, query):
		"""
		Resolve a DNS query via forwarding to upstream DoH server (POST).

		Params:
				query - wireformat DNS request packet

		Returns:
				A wireformat DNS response packet.

		Notes:
				Using DNS over HTTPS POST format as described here:
				https://tools.ietf.org/html/draft-ietf-doh-dns-over-https-12
				https://developers.cloudflare.com/1.1.1.1/dns-over-https/wireformat/
		"""

		# Select upstream to connect to
		upstream = self._select_upstream_rtt()

		# Forward request upstream
		try:
			rtt = time.monotonic()
			self._queries += 1
			upstream.queries += 1
			async with upstream.session.post(upstream.url, data=query) as http:
				# Log abnormal HTTP status codes
				if http.status != 200:
					logging.warning('HTTP error: %s (%d)' % (upstream.url, http.status))
					return b''

				# Wait for response
				answer = await http.read()
				rtt = time.monotonic() - rtt
				self._answers += 1
				upstream.answers += 1

				# Update estimated RTT for this upstream connection
				upstream.rtt = 0.875 * upstream.rtt + 0.125 * rtt

				# Reset Rtt every 1000 processed requests to prevent drift
				if self._answers % 1000 == 0:
					logging.info('Resolver statistics %s' % (self.get_stats()))
					for u in self._upstreams:
						logging.info(u.get_stats())
						u.rtt = 0.0

				# Return response
				return answer

		# Log exceptions
		except Exception as exc:
			logging.error('Client error: %s, %s' % (upstream.url, exc))
			upstream.rtt += 1.0
			return b''

	async def close(self):
		"""
		Close all upstream connections.
		"""

		for upstream in self._upstreams:
			await upstream.session.close()


class UdpDohProtocol(asyncio.DatagramProtocol):
	"""
	Protocol for serving UDP DNS requests via DNS over HTTPS.
	"""

	def __init__(self, resolver):
		self.resolver = resolver

	def connection_made(self, transport):
		self.transport = transport

	def connection_lost(self, exc):
		pass

	def datagram_received(self, data, addr):
		# Schedule ppacker forwarding coroutine
		asyncio.ensure_future(self.process_packet(addr, data))

	def error_received(self, exc):
		logging.warning('UDP transport error: %s' % (exc))

	async def process_packet(self, addr, query):
		# Resolve DNS query
		answer = await self.resolver.resolve(query)

		# Send DNS answer to client
		self.transport.sendto(answer, addr)


class TcpDohProtocol(asyncio.Protocol):
	"""
	Protocol for serving TCP DNS requests via DNS over HTTPS.
	"""

	def __init__(self, resolver):
		self.resolver = resolver

	def connection_made(self, transport):
		self.transport = transport

	def connection_lost(self, exc):
		if not self.transport.is_closing():
			self.transport.close()

	def data_received(self, data):
		asyncio.ensure_future(self.process_packet(data))

	def eof_received(self):
		return None

	async def process_packet(self, query):
		# Resolve DNS query (remove 16-bit length prefix)
		answer = await self.resolver.resolve(query[2:])

		# Send DNS answer to client (add 16-bit length prefix)
		self.transport.write(struct.pack('! H', len(answer)) + answer)


if __name__ == '__main__':
	main()
