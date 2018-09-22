#!/usr/bin/env python3
import asyncio, aiohttp
import argparse, logging
import struct, time
import urllib.parse
import dns.message
import dns.resolver


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
	parser.add_argument('--no-cache', action='store_true', default=False,
						help='don\'t cache answers from upstream servers (default: %(default)s)')
	parser.add_argument('--active-cache', action='store_true', default=False,
						help='actively replace expired entries by making autonomous requests to the upstream servers (default: %(default)s)')
	args = parser.parse_args()

	headers = {'accept': 'application/dns-message', 'content-type': 'application/dns-message'}

	# Setup logging
	logging.basicConfig(level='INFO', format='[%(levelname)s] %(message)s')

	# Setup event loop
	loop = asyncio.get_event_loop()

	# Setup cache if necessary
	if not args.no_cache:
		logging.info('Using DNS cache with %d capacity' % (50000))
		cache = dns.resolver.LRUCache(50000)

		# Report cache status every 12 hours
		loop.call_later(12*3600, cache_reporter, cache, 12*3600)
	else:
		cache = None

	# Setup DNS resolver to cache/forward queries and answers
	resolver = DohResolver(cache=cache)

	# Connect to upstream servers
	logging.info('Connecting to upstream servers: %r' % (args.upstreams))
	loop.run_until_complete(resolver.connect([(upstream, headers) for upstream in args.upstreams]))

	# Setup listening transports
	transports = []
	for addr in args.listen_address:
		for port in args.listen_port:
			# Setup UDP server
			logging.info('Starting UDP server listening on %s#%d' % (addr, port))
			udp_listen = loop.create_datagram_endpoint(lambda: UdpDohProtocol(resolver), local_addr = (addr, port))
			udp, protocol = loop.run_until_complete(udp_listen)
			transports.append(udp)

			# Setup TCP server
			if args.tcp:
				logging.info('Starting TCP server listening on %s#%d' % (addr, port))
				tcp_listen = loop.create_server(lambda: TcpDohProtocol(resolver), addr, port)
				tcp = loop.run_until_complete(tcp_listen)
				transports.append(tcp)

	#FIXME: Rework this to be smarter (maybe period timeout and min ttl)
	# Setup cache worker
	if args.active_cache:
		asyncio.ensure_future(resolver.worker(10))

	# Serve forever
	try:
		loop.run_forever()
	except (KeyboardInterrupt, SystemExit):
		pass

	# Close upstream connections
	loop.run_until_complete(resolver.close())

	# Close listening servers and event loop
	for transport in transports:
		transport.close()

	loop.close()


class UdpDohProtocol(asyncio.DatagramProtocol):
	"""
	DNS over HTTPS UDP protocol to use with asyncio.
	"""

	def __init__(self, resolver):
		self.resolver = resolver
		super().__init__()

	def connection_made(self, transport):
		self.transport = transport

	def datagram_received(self, data, addr):
		asyncio.ensure_future(self.process_packet(data, addr))

	def error_received(self, exc):
		logging.warning('Minor transport error')

	async def process_packet(self, query, addr):
		# Resolve DNS query
		answer = await self.resolver.resolve(query)

		# Send DNS answer to client
		self.transport.sendto(answer, addr)


class TcpDohProtocol(asyncio.Protocol):
	"""
	DNS over HTTPS TCP protocol to use with asyncio.
	"""

	def __init__(self, resolver):
		self.resolver = resolver
		super().__init__()

	def connection_made(self, transport):
		self.transport = transport

	def data_received(self, data):
		asyncio.ensure_future(self.process_packet(data))

	def eof_received(self):
		if self.transport.can_write_eof():
			self.transport.write_eof()

	def connection_lost(self, exc):
		self.transport.close()

	async def process_packet(self, query):
		# Resolve DNS query (remove 16-bit length prefix)
		answer = await self.resolver.resolve(query[2:])

		# Send DNS answer to client (add 16-bit length prefix)
		self.transport.write(struct.pack('! H', len(answer)) + answer)


class DohConn:
	"""
	DNS over HTTPS upstream connection class.
	"""

	def __init__(self, upstream, headers=None):
		self.url = upstream
		self.parsed = urllib.parse.urlparse(self.url)
		self.headers = headers
		self.conn = None

	async def connect(self):
		connector = aiohttp.TCPConnector(keepalive_timeout=60, limit=0, limit_per_host=200, enable_cleanup_closed=True)
		self.conn = aiohttp.ClientSession(connector=connector, headers=self.headers)

	async def close(self):
		if self.conn:
			await self.conn.close()

	async def forward_post(self, query):
		"""
		Perform DNS over HTTPS lookup using POST method.

		Params:
			query - normal wireformat DNS query

		Returns:
			A normal wireformat DNS answer and status code.

		Notes:
			Using DNS over HTTPS POST format as described here:
			https://tools.ietf.org/html/draft-ietf-doh-dns-over-https-12
			https://developers.cloudflare.com/1.1.1.1/dns-over-https/wireformat/
		"""

		# Attempt to query the upstream server asynchronously
		try:
			async with self.conn.post(self.url, data=query) as http:
				if http.status == 200:
					return (await http.read(), True)

			# Log abnormal HTTP status codes
			logging.warning('%s (%d): IN %s, OUT %s' % (self.url, http.status, query, await http.read()))
			return (b'', False)

		# Log client connection errors (aiohttp should attempt to reconnect on next request)
		except aiohttp.ClientConnectionError as exc:
			logging.error('Client error, %s: %s' % (self.url, exc))
			return (b'', False)

		# Log request timeout errors
		except asyncio.TimeoutError as exc:
			logging.error('Timeout error, %s: %s' % (self.url, exc))
			return (b'', False)


class DohResolver:
	"""
	DNS over HTTPS resolver class.
	"""

	def __init__(self, cache=None):
		self.cache = cache
		self.conns = []

	async def connect(self, upstreams):
		"""
		Prepare connection objects corresponding to upstream servers.
		"""

		for (url, headers) in upstreams:
			conn = DohConn(url, headers=headers)
			await conn.connect()
			self.conns.append(conn)

	async def close(self):
		"""
		Close connections to upstream servers.
		"""

		for conn in self.conns:
			await conn.close()

	async def resolve(self, query, update=False):
		"""
		Resolve a DNS query using a cache or upstream server.

		Params:
			query  - normal wireformat DNS query
			update - do not check cache for this query

		Returns:
			A normal wireformat DNS answer.
		"""

		# Check cache if necessary
		if self.cache and not update:
			# Convert wireformat to message object
			request = dns.message.from_wire(query)
			id = request.id
			request = request.question[0]
			request = (request.name, request.rdtype, request.rdclass)

			# Return cached entry if possible
			cached = self.cache.get(request)
			if cached:
				cached.response.id = id
				answer = cached.response.to_wire()
				return answer

		# Resolve via upstream server
		answer = await self.forward(query)

		# Add answer to cache if necessary
		if self.cache:
			if update:
				# Convert wireformat to message object
				request = dns.message.from_wire(query)
				id = request.id
				request = request.question[0]
				request = (request.name, request.rdtype, request.rdclass)

			response = dns.message.from_wire(answer)
			response = dns.resolver.Answer(*request, response, False)
			self.cache.put(request, response)

		return answer

	async def forward(self, query, timeout=0):
		"""
		Attempt to resolve DNS request through forwarding to upstream servers.

		Params:
			query - A normal wireformat DNS query

		Returns:
			A normal wireformat DNS answer.
		"""

		# Cycle through upstream servers
		index = 0
		while True:
			conn = self.conns[index]
			answer, status = await conn.forward_post(query)

			if status:
				break

			index = (index + 1) % len(self.conns)

		return answer

	async def worker(self, period, max=1000):
		"""
		Worker to process cache entries and preemptively replace expiring entries.

		Params:
			period - time to wait between cache scans (in seconds)
			max    - maximum number of concurrent autonomous requests (0 means unlimited)

		"""

		while True:
			expiring = 0

			for request, response in self.cache.data.items():
				if response.value.expiration > (time.time() + period):
					continue

				query = dns.message.make_query(*request).to_wire()
				asyncio.ensure_future(self.resolve(query, True))
				expiring += 1

				if max and expiring >= max:
					break

			if expiring > 0:
				logging.info('Cache, updating %d/%d' % (expiring, len(self.cache.data)))

			await asyncio.sleep(period)

def cache_reporter(cache, period):
	"""
	Worker used to log cache statistics at regular intervals.

	Params:
		cache  - cache to monitor and scan
		period - time to wait between cache scans (in seconds)
	"""

	loop = asyncio.get_event_loop()

	count = len(cache.data)
	size = cache.max_size

	logging.info('Cache status: %d/%d entries' % (count, size))

	loop.call_later(period, cache_reporter, cache, period)


if __name__ == '__main__':
	main()
