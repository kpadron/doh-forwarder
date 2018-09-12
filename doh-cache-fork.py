#!/usr/bin/env python3
import asyncio
import types, time
import random, struct
import argparse, logging
import dns.resolver
import dns.query
import dns.message
import dns.name
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
parser.add_argument('--timeout', type=float, default=3.0,
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
	p1 = mp.Process(target=cache_worker, args=(cache, wait_table, cache_request, cache_response, forwarder_request, forwarder_response), daemon=True)
	workers.append(p1)
	p2 = mp.Process(target=forwarder_worker, args=(('1.1.1.1', 53), timeout, forwarder_request, forwarder_response), daemon=True)
	workers.append(p2)
	if active:
		p3 = mp.Process(target=active_cache, args=(cache, 10, forwarder_request), daemon=True)
		workers.append(p3)

	# Setup event loop
	loop = asyncio.get_event_loop()

	# Setup UDP server
	logging.info('Starting UDP server listening on: %s#%d' % (host, port))
	udp_listen = loop.create_datagram_endpoint(UdpDnsListen, local_addr = (host, port))
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
	DNS over UDP.
	"""

	def connection_made(self, transport):
		self.transport = transport

	def datagram_received(self, data, addr):
		asyncio.ensure_future(self.resolve_packet(data, addr))

	def error_received(self, exc):
		logging.warning('Minor transport error')

	async def resolve_packet(self, query, addr):
		# Post query to cache -> (query, addr)
		logging.debug('listener: Cache POST %s' % (addr[0]))
		await cache_request.coro_put((query, addr))

		# Get response from cache <- (answer, addr)
		try:
			answer, addr = await asyncio.wait_for(cache_response.coro_get(), timeout)
		except asyncio.TimeoutError:
			answer = b''

		logging.debug('listener: Cache GET %s' % (addr[0]))

		# Send DNS packet to client
		self.transport.sendto(answer, addr)


def echo_worker(in_queue, out_queue):
	while True:
		request = in_queue.get()

		answer = dns.message.make_query(request.qname, request.qtype, request.qclass)

		response = dns.resolver.Answer(request.qname, request.qtype, request.qclass, answer, False)
		response.expiration = time.time() + 50

		out_queue.put((request, response))


class DnsCacheManager(multiprocessing.managers.BaseManager):
	pass


class DnsRequest:
	def __init__(self, qname, qtype, qclass):
		self.qname = qname
		self.qtype = qtype
		self.qclass = qclass


class DnsWaitTable:
	"""
	Synchronized table to store clients waiting on DNS requests.
	"""

	def __init__(self, lock=None):
		self.table = {}
		self.lock = lock

		if self.lock is None:
			self.lock = threading.Lock()

	def get(self, key):
		try:
			return self.table[key]
		except KeyError as exc:
			raise exc

	def set(self, key, value):
		self.table[key] = value

	def delete(self, key):
		try:
			del self.table[key]
		except KeyError:
			raise KeyError


class DnsLruCache(dns.resolver.LRUCache):
	"""
	Synchronized DNS LRU cache.
	"""

	def get(self, key):
		"""
		Returns value associated with key with ttl corrected.
		"""

		with self.lock:
			# Attempt to lookup data
			node = self.data.get(key)

			if node is None:
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

			return node.value

	def expired(self, timeout):
		"""
		Returns list of expired or almost expired cache entries.
		"""

		expired = []

		with self.lock:
			for k, v in self.data.items():
				if v.value.expiration <= time.time() + timeout:
					expired.append(k)

		return expired


def cache_worker(cache, wait_table, in_queue, out_queue, next_in, next_out):
	loop = asyncio.new_event_loop()
	asyncio.set_event_loop(loop)
	asyncio.ensure_future(cache_async_read(cache, wait_table, in_queue, out_queue, next_in))
	asyncio.ensure_future(cache_async_write(cache, wait_table, out_queue, next_out))
	loop.run_forever()


async def cache_async_read(cache, wait_table, in_queue, out_queue, next_in):
	while True:
		# Get query from client <- (query, addr)
		query, addr = await in_queue.coro_get()
		request = dns.message.from_wire(query)
		id = request.id
		request = request.question[0]
		request = DnsRequest(request.name, request.rdtype, request.rdclass)
		logging.debug('cache_recv: Client GET %s' % (request.qname))

		# Answer query from cache if possible
		response = cache.get((request.qname, request.qtype, request.qclass))
		if response is not None:
			response.response.id = id
			answer = response.response.to_wire()

			# Post response to client -> (answer, addr)
			await out_queue.coro_put((answer, addr))
			logging.debug('cache_recv: Client POST %s' % (request.qname))
			continue

		# Add client to wait list for this query
		try:
			with wait_table.lock:
				wait_list = wait_table.get((request.qname, request.qtype, request.qclass))
				if (id, addr) not in wait_list:
					wait_list.append((id, addr))

		# No outstanding requests for this query so create wait list and forward request
		except KeyError:
			with wait_table.lock:
				wait_table.set((request.qname, request.qtype, request.qclass), [(id, addr)])

			# Post query to forwarder -> (request)
			await next_in.coro_put(request)
			logging.debug('cache_recv: Forwarder POST %s' % (request.qname))


async def cache_async_write(cache, wait_table, out_queue, next_out):
	while True:
		# Get response from the forwarder <- (request, response)
		request, response = await next_out.coro_get()
		logging.debug('cache_send: Forwarder GET %s' % (request.qname))

		# Add entry to cache
		if response is None:
			try:
				with wait_table.lock:
					wait_table.delete((request.qname, request.qtype, request.qclass))
			except KeyError:
				pass

			continue

		cache.put((request.qname, request.qtype, request.qclass), response)

		# Reply to clients waiting for this query
		try:
			with wait_table.lock:
				reply_list = wait_table.get((request.qname, request.qtype, request.qclass))

			for (id, addr) in reply_list:
				response.response.id = id
				answer = response.response.to_wire()

				# Post response to client -> (answer, addr)
				await out_queue.coro_put((answer, addr))
				logging.debug('cache_send: Client POST %s' % (request.qname))

			with wait_table.lock:
				wait_table.delete((request.qname, request.qtype, request.qclass))
		except KeyError:
			pass


def forwarder_worker(upstream, timeout, in_queue, out_queue):
	loop = asyncio.new_event_loop()
	asyncio.set_event_loop(loop)

	sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, 0)
	sock.setblocking(0)
	loop.run_until_complete(loop.sock_connect(sock, upstream))

	asyncio.ensure_future(forwarder_async(sock, timeout, in_queue, out_queue))
	# asyncio.ensure_future(forwarder_async_write(upstream, timeout, in_queue, out_queue))
	loop.run_forever()


async def forwarder_async(sock, timeout, in_queue, out_queue):
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


def forwarder(upstream, timeout, in_queue, out_queue):
	"""
	Worker to process and forward requests and responses.

	Params:
		upstream  - remote address to forward requests to and receive responses from
		timeout   - time to wait before a request is invalid and cancelled
		in_queue  - queue object to receive requests from (synchronized)
		out_queue - queue object to send responses to (synchronized)
	"""

	while True:
		# Get request from cache <- (request)
		request = in_queue.get()
		logging.debug('forwarder: Cache GET %s' % (request.qname))

		# Resolve query
		query = dns.message.make_query(request.qname, request.qtype, request.qclass)
		try:
			response = udp_forward(None, upstream, query, timeout)
		except dns.exception.Timeout:
			logging.warning('forwarder: Query TIMEOUT %s' % (request.qname))
			continue

		response = dns.resolver.Answer(request.qname, request.qtype, request.qclass, response, False)

		# Post response to cache -> (request, response)
		out_queue.put((request, response))
		logging.debug('forwarder: Cache POST %s' % (request.qname))


def active_cache(cache, period, out_queue):
	"""
	Worker to process cache entries and preemptively replace expired or almost expired entries.

	Params:
		cache     - cache object to store data in for quick retrieval (synchronized)
		period    - time to wait between cache scans (in seconds)
		out_queue - queue object to send requests for further processing (synchronized)
	"""

	while True:
		expired = cache.expired(timeout)

		for key in expired:
			request = DnsRequest(*key)
			out_queue.put(request)

		if len(expired) > 0:
			logging.info('active_cache: Updated %d/%d entries' % (len(expired), len(cache.data)))

		time.sleep(period)


class TcpDnsListen(asyncio.Protocol):
	"""
	DNS over TCP protocol.
	"""

	def connection_made(self, transport):
		self.transport = transport

	def data_received(self, data):
		asyncio.ensure_future(self.resolve_packet(data))

	def eof_received(self):
		if self.transport.can_write_eof():
			self.transport.write_eof()

	def connection_lost(self, exc):
		self.transport.close()


async def udp_request(sock, query, timeout):
	loop = asyncio.get_event_loop()

	start = time.time()
	await loop.sock_sendall(sock, query)

	try:
		answer = await asyncio.wait_for(loop.sock_recv(sock, 65535), timeout)
	except asyncio.TimeoutError:
		return b'', -1

	if start is None:
		rtt = 0
	else:
		rtt = time.time() - start

	return answer, rtt

def udp_forward(sock, upstream, request, timeout):
	if not isinstance(request, bytes):
		request = request.to_wire()

		s = sock

		if s is None:
			s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, 0)
			s.setblocking(0)
			s.bind(('', 0))

		try:
			start = time.time()
			expiration = start + timeout
			dns.query._wait_for_writable(s, expiration)
			s.sendto(request, upstream)

			while 1:
				dns.query._wait_for_readable(s, expiration)
				response, addr = s.recvfrom(65535)
				if addr == upstream:
					break

		finally:
			if start is None:
				rtt = 0
			else:
				rtt = time.time() - start

			s.close()

		response = dns.message.from_wire(response)
		response.time = rtt

		return response


def tcp_forward(sock, upstream, request, timeout):
	if not isinstance(request, bytes):
		request = request.to_wire()

	s = sock

	if s is None:
		s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
		s.setblocking(0)
		s.bind(('', 0))

	try:
		start = time.time()
		expiration = start + timeout
		s.connect(upstream)
		dns.query.send_tcp(s, request, expiration)
		response, end = dns.query.receive_tcp(s, expiration)

	finally:
		if start is None or end is None:
			rtt = 0
		else:
			rtt = end - start

		s.close()

	response.time = rtt

	return response


if __name__ == '__main__':
	main()
