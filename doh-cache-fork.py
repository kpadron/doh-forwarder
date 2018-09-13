#!/usr/bin/env python3
import asyncio
import types, time
import random, struct
import argparse, logging
import dns.resolver
import dns.message
import socket
import threading
import multiprocessing as mp
import aioprocessing as aiomp
import multiprocessing.managers

# Attempt to use uvloop if installed for extra performance
# try:
# 	import uvloop
# 	asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
# except ImportError:
# 	pass


# Handle command line arguments
parser = argparse.ArgumentParser()
parser.add_argument('-a', '--listen-address', default='localhost',
					help='address to listen on for DNS over HTTPS requests (default: %(default)s)')
parser.add_argument('-p', '--listen-port', type=int, default=53,
					help='port to listen on for DNS over HTTPS requests (default: %(default)s)')
parser.add_argument('-u', '--upstreams', nargs='+', default=['1.1.1.1', '1.0.0.1'],
					help='upstream servers to forward DNS queries and requests to (default: %(default)s)')
parser.add_argument('-t', '--tcp', action='store_true', default=False,
					help='serve TCP based queries and requests along with UDP (default: %(default)s)')
parser.add_argument('-m', '--max-cache-size', type=int, default=10000,
					help='maximum size of the cache in dns records (default: %(default)s)')
parser.add_argument('--active', action='store_true', default=False,
					help='actively replace expired cache entries by performing upstream requests (default: %(default)s)')
parser.add_argument('--timeout', type=float, default=5.0,
					help='time to wait before giving up on a request (default: %(default)s seconds)')
args = parser.parse_args()

host = args.listen_address
port = args.listen_port
upstreams = args.upstreams
cache_size = args.max_cache_size
active = args.active
timeout = args.timeout

# Basic diagram
#           Q           Q
# listener -> cache {} -> forwarder
#           Q           Q

# Queue for listener to post requests and get responses
cache_request = aiomp.AioQueue()
cache_response = aiomp.AioQueue()
forwarder_request = aiomp.AioQueue()
forwarder_response = aiomp.AioQueue()


def main():
	# Setup logging
	logging.basicConfig(level='INFO', format='[%(levelname)s] %(message)s')

	# Setup resolver cache
	workers = []
	cache = DnsLruCache(cache_size)
	wait_table = DnsWaitTable()
	# p4 = mp.Process(target=echo_worker, args=(forwarder_request, forwarder_response), daemon=True)
	# workers.append(p4)
	p = mp.Process(target=cache_worker, args=(cache, wait_table, cache_request, cache_response, forwarder_request, forwarder_response), daemon=True)
	workers.append(p)
	p = mp.Process(target=forwarder_worker, args=(('1.1.1.1', 53), timeout, forwarder_request, forwarder_response), daemon=True)
	workers.append(p)

	# Setup event loop
	loop = asyncio.get_event_loop()

	# Setup UDP server
	logging.info('Starting UDP server listening on: %s#%d' % (host, port))
	udp_listen = loop.create_datagram_endpoint(lambda: UdpDnsListen(cache_response, cache_request), local_addr = (host, port))
	udp, protocol = loop.run_until_complete(udp_listen)

	# Setup TCP server
	if args.tcp:
		logging.info('Starting TCP server listening on %s#%d' % (host, port))
		tcp_listen = loop.create_server(TcpDnsListen, host, port)
		tcp = loop.run_until_complete(tcp_listen)

	# Serve forever
	try:
		for worker in workers:
			worker.start()

		loop.run_forever()
	except (KeyboardInterrupt, SystemExit):
		pass

	# Close listening servers and event loop
	udp.close()
	if args.tcp:
		tcp.close()

	loop.close()


class UdpDnsListen(asyncio.DatagramProtocol):
	"""
	DNS over UDP listener.
	"""

	def __init__(self, in_queue, out_queue, **kwargs):
		self.in_queue = in_queue
		self.out_queue = out_queue
		super().__init__(**kwargs)

	def connection_made(self, transport):
		self.transport = transport

	def datagram_received(self, data, addr):
		asyncio.ensure_future(self.process_packet(data, addr))

	def error_received(self, exc):
		logging.warning('Minor transport error')

	async def process_packet(self, query, addr):
		# Post query to cache -> (query, addr)
		logging.debug('LISTENER: Cache POST %s' % (addr[0]))
		self.out_queue.put((query, addr))

		# Get response from cache <- (answer, addr)
		answer, addr = await self.in_queue.coro_get()
		logging.debug('LISTENER: Cache GET %s' % (addr[0]))

		# Send DNS packet to client
		self.transport.sendto(answer, addr)

class TcpDnsListen(asyncio.Protocol):
	"""
	DNS over TCP listener.
	"""

	def connection_made(self, transport):
		self.transport = transport

	def data_received(self, data):
		asyncio.ensure_future(self.process_packet(data))

	def eof_received(self):
		if self.transport.can_write_eof():
			self.transport.write_eof()

	def connection_lost(self, exc):
		self.transport.close()

	async def process_packet(self, data):
		pass


class DnsRequest:
	"""
	DNS request object used for associating responses.
	"""

	def __init__(self, qname, qtype, qclass):
		self.qname = qname
		self.qtype = qtype
		self.qclass = qclass

class DnsWaitTableEntry:
	"""
	DNS waiting table entry.
	"""

	def __init__(self, start, wait_list=None):
		self.start = start
		self.wait_list = wait_list

		if self.wait_list is None:
			self.wait_list = []

class DnsWaitTable:
	"""
	DNS waiting table to store clients waiting on DNS requests.
	"""

	def __init__(self, lock=None):
		self.table = {}
		self.lock = lock

		if self.lock is None:
			self.lock = threading.Lock()

	def get(self, key):
		return self.table.get(key)

	def set(self, key, value):
		self.table[key] = value

	def delete(self, key):
		try:
			del self.table[key]
		except KeyError:
			pass

class DnsLruCacheNode:
	"""
	DNS LRU cache entry.
	"""

	def __init__(self, key, value):
		self.key = key
		self.value = value
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


class DnsLruCache:
	"""
	DNS LRU cache to store recently processes lookups.
	"""

	def __init__(self, size=100000):
		self.data = {}
		self.sentinel = DnsLruCacheNode(None, None)
		self.hits = 0
		self.misses = 0
		self.size = size

		if self.size < 1:
			self.size = 1

	def get(self, key):
		"""
		Returns value associated with key.
		"""

		# Attempt to lookup data
		node = self.data.get(key)

		if node is None:
			self.misses += 1
			return None

		# Unlink because we're either going to move the node to the front
		# of the LRU list or we're going to free it.
		node.unlink()

		# Check if data is expired
		if node.value.expiration <= time.time():
			del self.data[node.key]
			return None

		node.link_after(self.sentinel)

		# Return data with updated ttl
		response = node.value.response
		ttl = int(node.value.expiration - time.time())
		for section in (response.answer, response.authority, response.additional):
			for rr in section:
				rr.ttl = ttl

		self.hits += 1
		return node.value

	def put(self, key, value):
		"""
		Associate key and value in the cache.
		"""

		node = self.data.get(key)

		# Remove previous entry in this position
		if node is not None:
			node.unlink()
			del self.data[node.key]

		# Clean out least used entries if necessary
		while len(self.data) >= self.size:
			node = self.sentinel.prev
			node.unlink()
			del self.data[node.key]

		# Add entry to cache
		node = DnsLruCacheNode(key, value)
		node.link_after(self.sentinel)
		self.data[key] = node

	def flush(self, key=None):
		"""
		Flush the cache of entries.
		"""

		# Flush only key if given
		if key is not None:
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
			self.data = {}
			self.hits = 0
			self.misses = 0

	def ratio(self):
		"""
		Return cache hit ratio since creation or last full flush.
		"""

		if (self.hits + self.misses) > 0:
			return self.hits / (self.hits + self.misses)
		else:
			return 0

	def expired(self, timeout):
		"""
		Returns list of expired or almost expired cache entries.
		"""

		expired = []

		for k, v in self.data.items():
			if v.value.expiration <= time.time() + timeout:
				expired.append(k)

		return expired


################################################################################
def cache_worker(cache, wait_table, in_queue, out_queue, next_in, next_out):
	loop = asyncio.new_event_loop()
	asyncio.set_event_loop(loop)
	asyncio.ensure_future(cache_async_read(cache, wait_table, in_queue, out_queue, next_in))
	asyncio.ensure_future(cache_async_write(cache, wait_table, out_queue, next_out))
	if active:
		# threading.Thread(target=active_cache, args=(cache, 10, next_in), daemon=True).start()
		asyncio.ensure_future(active_cache_async(cache, 10, next_in))
	loop.run_forever()

async def cache_async_read(cache, wait_table, in_queue, out_queue, next_in):
	while True:
		# Get query from client <- (query, addr)
		query, addr = await in_queue.coro_get()
		request = dns.message.from_wire(query)
		id = request.id
		request = request.question[0]
		request = DnsRequest(request.name, request.rdtype, request.rdclass)
		logging.debug('CACHE: Client GET %s' % (request.qname))

		# Answer query from cache if possible
		response = cache.get((request.qname, request.qtype, request.qclass))
		if response is not None:
			response.response.id = id
			answer = response.response.to_wire()

			# Post response to client -> (answer, addr)
			logging.debug('CACHE: Client POST %s' % (request.qname))
			out_queue.put((answer, addr))
			continue

		# Add client to wait list for this query
		entry = wait_table.get((request.qname, request.qtype, request.qclass))

		# Create new wait list for this query and submit request
		if entry is None:
			entry = DnsWaitTableEntry(time.time(), [(addr, id)])
			wait_table.set((request.qname, request.qtype, request.qclass), entry)

			# Post query to forwarder -> (request)
			logging.debug('CACHE: Forwarder POST %s' % (request.qname))
			next_in.put(request)

		# Request is pending so add client to wait list
		else:
			# # Query has expired so reset wait list
			# if (entry.start + timeout) <= time.time():
			# 	raise KeyError

			# Check if client is already waiting
			wait_list = entry.wait_list
			for i, entry in enumerate(wait_list):
				# Use ID of latest request
				if entry[0] == addr:
					wait_list[i] = (addr, id)
					continue

			# Add client to wait list
			wait_list.append((addr, id))

async def cache_async_write(cache, wait_table, out_queue, next_out):
	while True:
		# Get response from the forwarder <- (request, response)
		request, response = await next_out.coro_get()
		logging.debug('CACHE: Forwarder GET %s' % (request.qname))

		# Add entry to cache
		cache.put((request.qname, request.qtype, request.qclass), response)

		# Reply to clients waiting for this query
		entry = wait_table.get((request.qname, request.qtype, request.qclass))

		# No clients are waiting on this query
		if entry is None:
			continue

		# Clients are waiting so create and send replies
		reply_list = entry.wait_list
		for (addr, id) in reply_list:
			# Prepare answer to query
			response.response.id = id
			answer = response.response.to_wire()

			# Post response to client -> (answer, addr)
			out_queue.put((answer, addr))
			logging.debug('CACHE: Client POST %s' % (request.qname))

		# Remove wait list for this query
		wait_table.delete((request.qname, request.qtype, request.qclass))

async def active_cache_async(cache, period, out_queue):
	"""
	Worker to process cache entries and preemptively replace expired or almost expired entries.

	Params:
		cache     - cache object to store data in for quick retrieval (synchronized)
		period    - time to wait between cache scans (in seconds)
		out_queue - queue object to send requests for further processing (synchronized)
	"""

	while True:
		expired = cache.expired(period)

		for key in expired:
			request = DnsRequest(*key)
			out_queue.put(request)

		if len(expired) > 0:
			logging.info('CACHE: Updated %d/%d entries' % (len(expired), len(cache.data)))
			logging.info('CACHE: Hits %d, Misses %d, Ratio %.2f' % (cache.hits, cache.misses, cache.ratio()))

		await asyncio.sleep(period)
################################################################################


################################################################################
class UdpDnsForward(asyncio.DatagramProtocol):
	"""
	DNS over UDP forwarder.
	"""

	def __init__(self, out_queue):
		self.out_queue = out_queue
		super().__init__()

	def connection_made(self, transport):
		self.transport = transport

	def datagram_received(self, data, addr):
		asyncio.ensure_future(self.process_packet(data))

	async def process_packet(self, answer):
		answer = dns.message.from_wire(answer)
		request = DnsRequest(answer.question[0].name, answer.question[0].rdtype, answer.question[0].rdclass)
		response = dns.resolver.Answer(request.qname, request.qtype, request.qclass, answer, False)

		logging.debug('FORWARDER: Cache POST %s' % (request.qname))
		self.out_queue.put((request, response))

def forwarder_worker(upstream, timeout, in_queue, out_queue):
	loop = asyncio.new_event_loop()
	asyncio.set_event_loop(loop)

	# Connect to remote server
	udp_forward = loop.create_datagram_endpoint(lambda: UdpDnsForward(out_queue), remote_addr=upstream)
	transport, protocol = loop.run_until_complete(udp_forward)

	asyncio.ensure_future(forwarder_async(transport, in_queue))
	loop.run_forever()

async def forwarder_async(transport, in_queue):
	while True:
		request = await in_queue.coro_get()
		logging.debug('FORWARDER: Cache GET %s' % (request.qname))

		query = dns.message.make_query(request.qname, request.qtype, request.qclass)
		query = query.to_wire()

		transport.sendto(query)
################################################################################

async def forwarder_async2(sock, timeout, in_queue, out_queue):
	while True:
		request = await in_queue.coro_get()
		logging.debug('forwarder: Cache GET %s' % (request.qname))

		query = dns.message.make_query(request.qname, request.qtype, request.qclass)
		query = query.to_wire()

		answer, rtt = await udp_request(sock, query, timeout)

		if answer == b'':
			response = None
		else:
			answer = dns.message.from_wire(answer)
			response = dns.resolver.Answer(request.qname, request.qtype, request.qclass, answer, False)

		await out_queue.coro_put((request, response))
		logging.debug('forwarder: Cache POST %s' % (request.qname))


if __name__ == '__main__':
	main()
