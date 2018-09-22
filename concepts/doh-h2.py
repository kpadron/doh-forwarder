#!/usr/bin/env python3
import asyncio, aiohttp
import concurrent.futures
import random, struct
import argparse, logging
import dns.message
import dns.resolver
import time
import requests
import hyper
import urllib.parse
import aioh2
import ssl

# Attempt to use uvloop if installed for extra performance
try:
	import uvloop
	asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
	pass

# Handle command line arguments
parser = argparse.ArgumentParser()
parser.add_argument('-a', '--listen-address', default='127.0.0.1',
					help='address to listen on for DNS over HTTPS requests (default: %(default)s)')
parser.add_argument('-p', '--listen-port', type=int, default=53,
					help='port to listen on for DNS over HTTPS requests (default: %(default)s)')
parser.add_argument('-u', '--upstreams', nargs='+', default=['https://1.1.1.1/dns-query', 'https://1.0.0.1/dns-query'],
					help='upstream servers to forward DNS queries and requests to (default: %(default)s)')
parser.add_argument('-t', '--tcp', action='store_true', default=False,
					help='serve TCP based queries and requests along with UDP (default: %(default)s)')
parser.add_argument('--no-cache', action='store_true', default=False,
					help='don\'t cache answers from upstream servers (default: %(default)s)')
parser.add_argument('--active-cache', action='store_true', default=False,
					help='actively replace expired entries by making autonomous requests to the upstream servers (default: %(default)s)')
args = parser.parse_args()

host = args.listen_address
port = args.listen_port
upstreams = args.upstreams
headers = {'accept': 'application/dns-message', 'content-type': 'application/dns-message'}


def main():
	# Setup logging
	logging.basicConfig(level='INFO', format='[%(levelname)s] %(message)s')

	# Setup event loop
	loop = asyncio.get_event_loop()
	# executor = concurrent.futures.ProcessPoolExecutor(max_workers=4)
	# executor = concurrent.futures.ThreadPoolExecutor(max_workers=1000)
	# loop.set_default_executor(executor)

	# Connect to upstream servers
	logging.info('Connecting to upstream servers: %r' % (upstreams))
	resolver = DohResolver(cache=dns.resolver.LRUCache(50000))
	# resolver.connect([(upstream, headers) for upstream in upstreams])
	loop.run_until_complete(resolver.connect([(upstream, headers) for upstream in upstreams]))

	# Setup UDP server
	logging.info('Starting UDP server listening on: %s#%d' % (host, port))
	udp_listen = loop.create_datagram_endpoint(lambda: UdpDohProtocol(resolver), local_addr = (host, port))
	udp, protocol = loop.run_until_complete(udp_listen)

	# Setup TCP server
	if args.tcp:
		logging.info('Starting TCP server listening on %s#%d' % (host, port))
		tcp_listen = loop.create_server(lambda: TcpDohProtocol(resolver), host, port)
		tcp = loop.run_until_complete(tcp_listen)

	# Setup cache worker
	if args.active_cache:
		asyncio.ensure_future(resolver.worker(10))

	# Serve forever
	try:
		loop.run_forever()
	except (KeyboardInterrupt, SystemExit):
		pass

	# Close upstream connections
	# resolver.close()
	loop.run_until_complete(resolver.close())

	# Close listening servers and event loop
	udp.close()
	if args.tcp:
		tcp.close()

	loop.close()


class UdpDohProtocol(asyncio.DatagramProtocol):
	"""
	DNS over HTTPS UDP protocol to use with asyncio.
	"""

	def __init__(self, resolver):
		self.resolver = resolver
		self.loop = asyncio.get_event_loop()
		super().__init__()

	def connection_made(self, transport):
		self.transport = transport

	def datagram_received(self, data, addr):
		asyncio.ensure_future(self.process_packet(data, addr))

	def error_received(self, exc):
		logging.warning('Minor transport error')

	async def process_packet(self, query, addr):
		# Await upstream forwarding coroutine
		# answer = await self.loop.run_in_executor(None, self.resolver.forward, query)
		# answer = self.resolver.resolve(query)
		# answer = await self.loop.run_in_executor(None, forward, 'https://1.1.1.1/dns-query', query, headers)
		answer = await self.resolver.resolve(query)

		# TODO: try loop.run_in_executor(ProcessPool, resolver, query)

		# Send DNS packet to client
		self.transport.sendto(answer, addr)


def forward(upstream, query, headers):
	while True:
		response = requests.post(upstream, data=query, headers=headers)
		if response.status_code == 200:
			return response.content


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
		# Await upstream forwarding coroutine (remove 16-bit length prefix)
		answer = await self.resolver.resolve(query[2:])

		# Send DNS packet to client (add 16-bit length prefix)
		self.transport.write(struct.pack('! H', len(data)) + data)


class DohConnAioh2:
	def __init__(self, upstream, headers):
		self.url = urllib.parse.urlparse(upstream)
		self.headers = headers
		self.conn = None
		self.rtt = None

	async def connect(self):
		self.conn = await aioh2.open_connection(self.url.netloc, 443, ssl=ssl.create_default_context(), functional_timeout=0.2)
		await asyncio.sleep(0.2)
		print(self.conn._transport)

	async def close(self):
		self.conn.close_connection()
		await asyncio.sleep(0.1)

	async def request(self, data, headers=None, end_stream=False):
		if not headers:
			headers = {':method': 'POST', ':path': self.url.path}

		self.rtt = await self.conn.wait_functional()
		sid = await self.conn.start_request({**self.headers, **headers})
		await self.conn.send_data(sid, data, end_stream=end_stream)
		return sid


class DohConn:
	"""
	DNS over HTTPS upstream connection class.
	"""

	def __init__(self, upstream, headers):
		self.url = urllib.parse.urlparse(upstream)
		self.headers = headers
		self.conn = None

	def connect(self):
		self.conn = hyper.HTTP20Connection(self.url.netloc, port=443, secure=True)
		self.conn.connect()

	def close(self):
		self.conn.close()

	def request(self, method, body=None, headers=None):
		if not headers:
			headers = self.headers

		return self.conn.request(method, self.url.path, body=body, headers=headers)

	def response(self, sid=None):
		return self.conn.get_response(stream_id=sid)

class DohResolver:
	"""
	DNS over HTTPS resolver class.
	"""

	def __init__(self, cache=None):
		self.conns = []
		self.cache = cache

	async def connect(self, upstreams):
		"""
		Prepare connection object corresponding to upstream servers.
		"""

		for (url, headers) in upstreams:
			# connection = DohConn(url, headers)
			# connection.connect()
			connection = DohConnAioh2(url, headers)
			await connection.connect()
			self.conns.append(connection)

	async def close(self):
		"""
		Close connection to upstream server.
		"""

		for conn in self.conns:
			# conn.close()
			await conn.close()

	async def resolve(self, query, update=False):
		"""
		Resolve a DNS query using a cache or upstream server.

		Params:
			query  - normal wireformat DNS query
			update - only update cache entry for this query

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
		# answer = self.forward(query)
		answer = await self.forward_aioh2(query)

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

	def forward(self, query):
		"""
		Perform DNS over HTTPS lookup using POST method.

		Params:
			query - normal wireformat DNS query

		Returns:
			A normal wireformat DNS answer.

		Notes:
			Using DNS over HTTPS POST format as described here:
			https://tools.ietf.org/html/draft-ietf-doh-dns-over-https-12
			https://developers.cloudflare.com/1.1.1.1/dns-over-https/wireformat/
		"""

		# Await upstream response
		index = 0
		while True:
			sid = self.conns[index].request('POST', body=query)
			http = self.conns[index].response(sid=sid)

			if http.status == 200:
				return http.read()

			index = (index + 1) % len(self.conns)

	async def forward_aioh2(self, query):
		index = 0
		while True:
			conn = self.conns[index]
			sid = await conn.request(query, end_stream=True)
			http = await conn.conn.recv_response(sid)
			print(http)
			return await conn.conn.read_stream(sid, -1)

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


if __name__ == '__main__':
	main()
