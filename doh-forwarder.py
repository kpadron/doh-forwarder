#!/usr/bin/env python3
import asyncio, aiohttp
import argparse, logging
import struct, time
import concurrent.futures
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
	parser.add_argument('--cache-size', type=int, default=50000,
						help='maximum number of concurrent entries to cache (default: %(default)s)')
	parser.add_argument('--active-cache', action='store_true', default=False,
						help='actively replace expired entries by making autonomous requests to the upstream servers (default: %(default)s)')
	parser.add_argument('--ttl-bias', type=int, default=0,
						help='ttl bias in seconds, negative values improve caching behavior and positive values reduce staleness (default: %(default)s)')
	parser.add_argument('--min-ttl', type=int, default=0,
						help='minimum ttl used for cache entries regardless of ttl received from upstream (default: %(default)s)')
	args = parser.parse_args()

	headers = {'accept': 'application/dns-message', 'content-type': 'application/dns-message'}

	# Setup logging
	logging.basicConfig(level='INFO', format='[%(levelname)s] %(message)s')
	logging.info('Starting DNS over HTTPS forwarder')
	logging.info('Args: %r' % (vars(args)))

	# Setup event loop
	loop = asyncio.get_event_loop()
	tasks = []

	# Setup cache if necessary
	cache = None
	if not args.no_cache:
		logging.info('Using DNS cache with a maximum of %d entries' % (args.cache_size))
		cache = DohCache(args.cache_size, args.min_ttl, args.ttl_bias)

		# Report cache status every 6 hours
		report_period = 6 #* 3600
		report_task = asyncio.ensure_future(cache.report_scheduler(report_period))
		tasks.append(report_task)

	# Setup DNS resolver to cache/forward queries and answers
	resolver = DohResolver(cache)

	# Periodically replace expired cache entries
	if cache and args.active_cache:
		update_period = 10
		update_task = asyncio.ensure_future(resolver.update_scheduler(update_period))
		tasks.append(update_task)

	# Connect to upstream servers
	logging.info('Connecting to upstream servers: %r' % (args.upstreams))
	if len(tasks) > 0:
		loop.set_default_executor(concurrent.futures.ProcessPoolExecutor(len(tasks)))
	loop.run_until_complete(resolver.connect((upstream, headers) for upstream in args.upstreams))

	# Setup listening transports
	transports = []
	for addr in args.listen_address:
		for port in args.listen_port:
			# Setup UDP server
			logging.info('Starting UDP server listening on %s#%d' % (addr, port))
			udp_listen = loop.create_datagram_endpoint(lambda: UdpDohProtocol(resolver), local_addr=(addr, port))
			udp, protocol = loop.run_until_complete(udp_listen)
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

	# Close running tasks
	for task in tasks:
		exc = task.exception()
		if exc:
			print('Task with exception: %s' % (exc))

		task.cancel()

	# Wait for operations to end and close event loop
	loop.run_until_complete(asyncio.sleep(0.3))
	loop.close()


class UdpDohProtocol(asyncio.DatagramProtocol):
	"""
	DNS over HTTPS UDP protocol to use with asyncio.
	"""

	def __init__(self, resolver):
		self.resolver = resolver

	def connection_made(self, transport):
		self.transport = transport

	def datagram_received(self, data, addr):
		asyncio.ensure_future(self.process_packet(data, addr))

	def error_received(self, exc):
		logging.warning('UDP transport error: %s' % (exc))

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

	def __init__(self, url, headers=None):
		"""
		Construct DohConn object.

		Params:
			upstream - full url of the upstream server (https://ip-address/path)
			headers  - headers to send with requests to this upstream server
		"""

		self.url = url
		self.parsed = urllib.parse.urlparse(self.url)
		self.headers = headers
		self.conn = None

	async def connect(self):
		connector = aiohttp.TCPConnector(keepalive_timeout=60, limit=0, limit_per_host=200, enable_cleanup_closed=True)
		self.conn = aiohttp.ClientSession(connector=connector, headers=self.headers)

	async def close(self):
		if self.conn:
			await self.conn.close()
			await asyncio.sleep(0.3)

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
				logging.warning('HTTP error: %s (%d), %s' % (self.url, http.status,
								dns.message.from_wire(query).to_text()))
				return (b'', False)

		# Log client connection errors (aiohttp should attempt to reconnect on next request)
		except aiohttp.ClientConnectionError as exc:
			logging.error('Client error: %s, %s' % (self.url, exc))
			return (b'', False)

		# Log request timeout errors
		except asyncio.TimeoutError as exc:
			logging.error('Timeout error: %s, %s' % (self.url, exc))
			return (b'', False)


class DohResolver:
	"""
	DNS over HTTPS asynchronous resolver class.
	"""

	def __init__(self, cache=None):
		"""
		Construct DohResolver object.

		Params:
			cache    - cache object to store responses in
		"""

		self.conns = []
		self.cache = cache

	async def connect(self, upstreams):
		"""
		Prepare connection objects corresponding to upstream servers.

		Params:
			upstreams - iterable of (url, headers) tuples
		"""

		for (url, headers) in upstreams:
			conn = DohConn(url, headers)
			await conn.connect()
			self.conns.append(conn)

	async def close(self):
		"""
		Close connections to upstream servers.
		"""

		for conn in self.conns:
			await conn.close()

	async def resolve(self, query, update_only=False):
		"""
		Resolve a DNS query using a cache or upstream server.

		Params:
			query       - normal wireformat DNS query
			update_only - do not check cache for this query

		Returns:
			A normal wireformat DNS answer.
		"""

		# Check cache if necessary
		if self.cache:
			# Convert wireformat to message object
			request = dns.message.from_wire(query)
			id = request.id
			request = request.question[0]
			request = (request.name, request.rdtype, request.rdclass)

			# Return cached entry if possible
			if not update_only:
				cached, expiration = self.cache.get(request)

				if cached is not None:
					cached.id = id
					self.fix_ttl(cached, expiration)
					answer = cached.to_wire()
					return answer

		# Resolve via upstream server
		answer = await self.forward(query)

		# Add answer to cache if necessary
		if self.cache and answer:
			response = dns.message.from_wire(answer)
			expiration = dns.resolver.Answer(*request, response, False).expiration
			self.cache.put(request, response, expiration)

		return answer

	async def forward(self, query):
		"""
		Attempt to resolve DNS request by forwarding to upstream servers.

		Params:
			query - A normal wireformat DNS query

		Returns:
			A normal wireformat DNS answer.
		"""

		# Cycle through upstream servers
		upstreams = len(self.conns)
		for index in range(upstreams * 2):
			conn = self.conns[index % upstreams]
			answer, status = await conn.forward_post(query)

			if status:
				return answer

		return b''

	def fix_ttl(self, response, expiration):
		"""
		Fixes ttl fields in relevant resource records in response.

		Params:
			response   - DNS response object to modify
			expiration - time at which this response is considered stale
		"""

		ttl = int(expiration - time.time())
		for section in (response.answer, response.authority, response.additional):
			for rr in section:
				if hasattr(rr, 'ttl'):
					rr.ttl = max(ttl, 0)

	async def update_scheduler(self, period):
		"""
		Worker used to process cache entries and preemptively replace expiring entries.

		Params:
			period - time to wait between cache scans (in seconds)
		"""

		try:
			loop = asyncio.get_event_loop()

			while True:
				await asyncio.sleep(period)

				expired = self.cache.expired(period * 2)

				if len(expired) <= 0:
					continue

				diff = time.time()
				updates = await loop.run_in_executor(None, update, [(conn.url, conn.headers) for conn in self.conns], expired)
				diff = time.time() - diff

				logging.info('Cache update: updated %d/%d expired entries in %.2f (s), %.2f (updates/s)' % (len(updates), len(expired), diff, len(updates) / diff))
				for (request, response, expiration) in updates:
					self.cache.put(request, response, expiration)

		except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
			pass


def update(upstreams, expired):
	"""
	Used to process cache entries and replace expiring entries.

	Params:
		upstreams - iterable of (url, headers) tuples
		expired   - iterable of cache request keys

	Returns:
		An iterable of (request, response, expiration) tuples.
	"""

	try:
		loop = asyncio.new_event_loop()

		sessions = []
		conns = []
		for (url, headers) in upstreams:
			connector = aiohttp.TCPConnector(keepalive_timeout=60, limit=0, limit_per_host=200, enable_cleanup_closed=True, loop=loop)
			session = aiohttp.ClientSession(connector=connector, headers=headers, loop=loop)
			sessions.append(session)
			conns.append((session, url))

		futures = []
		for request in expired:
			future = asyncio.ensure_future(update_forward(conns, request), loop=loop)
			futures.append(future)

		updated = []
		for future in futures:
			request, answer = loop.run_until_complete(future)

			if answer:
				response = dns.message.from_wire(answer)
				expiration = dns.resolver.Answer(*request, response, False).expiration
				updated.append((request, response, expiration))

		for session in sessions:
			loop.run_until_complete(session.close())

		loop.run_until_complete(loop.shutdown_asyncgens())
		loop.close()

		return tuple(updated)

	except (KeyboardInterrupt, SystemExit):
		pass

# FIXME: write proper documentation
async def update_forward(conns, request):
	"""
	Write proper documentation.
	"""

	query = dns.message.make_query(*request).to_wire()

	# Attempt to query the upstream server asynchronously
	upstreams = len(conns)
	for index in range(upstreams * 3):
		try:
			session, url = conns[index % upstreams]
			async with session.post(url, data=query) as http:
				if http.status == 200:
					return (request, await http.read())

		except aiohttp.ClientConnectionError:
			pass

		except asyncio.TimeoutError:
			pass

	return (request, b'')


class DohCacheNode:
	"""
	DNS over HTTPS LRU cache entry.

	Notes:
		Based on the dns.resolver.LRUCacheNode class from dnspython package.
	"""

	def __init__(self, key, value, expiration=None):
		"""
		Construct DohCacheNode object.

		Params:
			key        - identifier used to map entry value
			value      - object to store in cache
			expiration - time at which this entry is considered stale (value returned from time.time())
		"""

		self.key = key
		self.value = value
		self.expiration = expiration
		self.prev = self
		self.next = self

	def link_before(self, node):
		self.prev = node.prev
		self.next = node
		node.prev.next = self
		node.prev = self

	def link_after(self, node):
		self.prev = node
		self.next = node.next
		node.next.prev = self
		node.next = self

	def unlink(self):
		self.next.prev = self.prev
		self.prev.next = self.next


class DohCache:
	"""
	DNS over HTTPS LRU cache to store recently processed lookups (optionally synchronized).

	Notes:
		Based on the dns.resolver.LRUCache class from dnspython package.
	"""

	def __init__(self, size=50000, min_ttl=0, ttl_bias=0, lock=None):
		"""
		Construct DohCache object.

		Params:
			size     - max capacity of cache (in entries)
			min_ttl  - minimum ttl for cache entries
			ttl_bias - ttl offset used to bias cache behavior
			lock     - lock used to synchronize access to the cache

		Notes:
			If used lock must have acquire() and release() methods.
		"""

		self.hits = 0
		self.misses = 0
		self.data = {}
		self.sentinel = DohCacheNode(None, None)
		self.size = size
		self.min_ttl = min_ttl
		self.ttl_bias = ttl_bias
		self.lock = lock

		if self.size < 1:
			self.size = 1

		if self.min_ttl < 0:
			self.min_ttl = 0

	def get(self, key):
		"""
		Returns value associated with key.

		Params:
			key - identifier associated with requested value

		Returns:
			The (value, expiration) tuple associated with key.
		"""

		if self.lock:
			self.lock.acquire()

		try:
			# Attempt to lookup data
			node = self.data.get(key)

			if node is None:
				self.misses += 1
				return (None, None)

			# Unlink because we're either going to move the node to the front
			# of the LRU list or we're going to free it.
			node.unlink()

			# Check if data is expired
			if node.expiration is not None:
				now = time.time() + self.ttl_bias
				if now > node.expiration:
					self.misses += 1
					del self.data[node.key]
					return (None, None)

			self.hits += 1
			node.link_after(self.sentinel)

			# Return value and expiration info
			return (node.value, node.expiration)

		finally:
			if self.lock:
				self.lock.release()

	def put(self, key, value, expiration=None):
		"""
		Associate key and value in the cache.

		Params:
			key        - identifier used to map entry value
			value      - entry to store in the cache
			expiration - time at which this entry is considered stale (value returned from time.time())
		"""

		if self.lock:
			self.lock.acquire()

		try:
			node = self.data.get(key)

			# Remove previous entry in this position
			if node is not None:
				node.unlink()
				del self.data[node.key]

			# Clean out least recently used entries if necessary
			while len(self.data) >= self.size:
				node = self.sentinel.prev
				node.unlink()
				del self.data[node.key]

			# Adjust expiration if necessary
			now = time.time()
			if (expiration - now) < self.min_ttl:
				expiration = now + self.min_ttl

			# Add entry to cache
			node = DohCacheNode(key, value, expiration)
			node.link_after(self.sentinel)
			self.data[key] = node

		finally:
			if self.lock:
				self.lock.release()

	def flush(self, keys=None):
		"""
		Flush the cache of entries.

		Params:
			keys - flush only entries in this iterable if provided
		"""

		if self.lock:
			self.lock.acquire()

		try:
			# Flush only key if given
			if keys:
				for key in keys:
					node = self.data.get(key)

					if node is not None:
						node.unlink()
						del self.data[node.key]
			else:
				node = self.sentinel.next

				# Remove references to all entry nodes
				while node != self.sentinel:
					next = node.next
					node.prev = None
					node.next = None
					node = next

				# Reset cache
				self.hits = 0
				self.misses = 0
				self.data = {}

		finally:
			if self.lock:
				self.lock.release()

	def stats(self):
		"""
		Return cache statistics.

		Returns:
			A tuple of (entries, size, hits, misses).
		"""

		if self.lock:
			self.lock.acquire()

		try:
			return (len(self.data), self.size, self.hits, self.misses)

		finally:
			if self.lock:
				self.lock.release()

	def reset_stats(self):
		"""
		Reset cache statistics to their original values.
		"""

		if self.lock:
			self.lock.acquire()

		try:
			self.hits = 0
			self.misses = 0

		finally:
			if self.lock:
				self.lock.release()

	def expired(self, offset=0):
		"""
		Returns tuple of expired or almost expired cache entries.

		Params:
			offset - time to offset expiration checks (in seconds)

		Returns:
			A tuple of keys corresponding to expiring entries.

		Notes:
			When (offset > 0) more entries will be considered expired.
			When (offset < 0) fewer entries will be considered expired.
		"""

		if self.lock:
			self.lock.acquire()

		try:
			expired = []

			now = time.time()
			for (k, v) in self.data.items():
				if (now + offset) > v.expiration:
					expired.append(k)

			return tuple(expired)

		finally:
			if self.lock:
				self.lock.release()

	async def report_scheduler(self, period):
		"""
		Worker to log cache statistics at regular intervals.

		Params:
			period - time to wait between cache scans (in seconds)
		"""

		try:
			loop = asyncio.get_event_loop()

			while True:
				await asyncio.sleep(period)
				await loop.run_in_executor(None, report, self.stats())

		except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
			pass


def report(stats):
	"""
	Used to log cache statistics.
	"""

	try:
		count, size, hits, misses = stats

		if (hits + misses) > 0:
			logging.info('Cache statistics: using %d/%d entries, hit/miss %d/%d %.1f%%' % (count, size, hits, misses, hits / (hits + misses) * 100))

	except (KeyboardInterrupt, SystemExit):
		pass


if __name__ == '__main__':
	main()
