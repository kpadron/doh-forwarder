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
import queue
import janus

# Attempt to use uvloop if installed for extra performance
try:
	import uvloop
	asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
	pass


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
args = parser.parse_args()

host = args.listen_address
port = args.listen_port
upstreams = args.upstreams
cache_size = args.max_cache_size
active = args.active

# Basic diagram
#           Q           Q
# listener -> cache {} -> forwarder
#           Q           Q

# Queue for listener to post requests and get responses
cache_request = janus.Queue()
cache_response = janus.Queue()


def main():
	# Setup logging
	logging.basicConfig(level='INFO', format='[%(levelname)s] %(message)s')

	# Setup resolver cache
	workers = []
	cache = DnsLruCache(cache_size)
	forwarder_request = queue.Queue()
	forwarder_response = queue.Queue()
	t1 = threading.Thread(target=cache_recv, args=(cache, cache_request.sync_q, cache_response.sync_q, forwarder_request), daemon=True)
	workers.append(t1)
	t2 = threading.Thread(target=cache_send, args=(cache, forwarder_response, cache_response.sync_q), daemon=True)
	workers.append(t2)
	t3 = threading.Thread(target=forwarder, args=(('1.1.1.1', 53), 5, forwarder_request, forwarder_response), daemon=True)
	workers.append(t3)
	if active:
		t4 = threading.Thread(target=active_cache, args=(cache, 10, forwarder_request), daemon=True)
		workers.append(t4)

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
		await cache_request.async_q.put((query, addr))

		# Get response from cache <- (answer, addr)
		answer, addr = await cache_response.async_q.get()
		logging.debug('listener: Cache GET %s' % (addr[0]))

		# Send DNS packet to client
		self.transport.sendto(answer, addr)


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

	def get_lock(self, key):
		with self.lock:
			return self.get(key)

	def set_lock(self, key, value):
		with self.lock:
			self.set(key, value)

	def delete_lock(self, key):
		with self.lock:
			self.delete(key)


class DnsLruCache(dns.resolver.LRUCache):
	"""
	Synchronized DNS LRU cache.
	"""

	def __init__(self, *args, **kwargs):
		self.wait_table = DnsWaitTable()
		super().__init__(*args, **kwargs)

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


def cache_recv(cache, in_queue, out_queue, next_queue):
	"""
	Worker to process cache requests and forward requests to the next stage.

	Params:
		cache      - cache object to store data in for quick retrieval (synchronized)
		in_queue   - queue object to receive requests from (synchronized)
		out_queue  - queue object to send responses to (synchronized)
		next_queue - queue object to send requests for further processing (synchronized)
	"""

	while True:
		# Get query from client <- (query, addr)
		query, addr = in_queue.get()
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
			out_queue.put((answer, addr))
			logging.debug('cache_recv: Client POST %s' % (request.qname))
			continue

		# Add client to wait list for this query
		for _ in range(2):
			try:
				with cache.wait_table.lock:
					cache.wait_table.get((request.qname, request.qtype, request.qclass)).append((id, addr))

			# No outstanding requests for this query so create wait list and forward request
			except KeyError:
				with cache.wait_table.lock:
					cache.wait_table.set((request.qname, request.qtype, request.qclass), [])

				# Post query to forwarder -> (request)
				next_queue.put(request)
				logging.debug('cache_recv: Forwarder POST %s' % (request.qname))
			else:
				break


def cache_send(cache, in_queue, out_queue):
	"""
	Worker to process and cache replies and responses from next stage.

	Params:
		cache     - cache object to store data in for quick retrieval (synchronized)
		in_queue  - queue object to receive responses from (synchronized)
		out_queue - queue object to send responses to (synchronized)
	"""

	while True:
		# Get response from the forwarder <- (request, response)
		request, response = in_queue.get()
		logging.debug('cache_send: Forwarder GET %s' % (request.qname))

		# Add entry to cache
		cache.put((request.qname, request.qtype, request.qclass), response)

		# Reply to clients waiting for this query
		try:
			with cache.wait_table.lock:
				reply_list = cache.wait_table.get((request.qname, request.qtype, request.qclass))

				for (id, addr) in reply_list:
					response.response.id = id
					answer = response.response.to_wire()

					# Post response to client -> (answer, addr)
					out_queue.put((answer, addr))
					logging.debug('cache_send: Client POST %s' % (request.qname))

				cache.wait_table.delete((request.qname, request.qtype, request.qclass))
		except KeyError:
			pass


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


def active_cache(cache, timeout, out_queue):
	"""
	Worker to process cache entries and preemptively replace expired or almost expired entries.

	Params:
		cache     - cache object to store data in for quick retrieval (synchronized)
		timeout   - time to wait between cache scans (in seconds)
		out_queue - queue object to send requests for further processing (synchronized)
	"""

	while True:
		expired = cache.expired(timeout)

		for key in expired:
			request = DnsRequest(*key)
			out_queue.put(request)

		if len(expired) > 0:
			logging.info('active_cache: Updated %d/%d entries' % (len(expired), len(cache.data)))

		time.sleep(timeout)


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





class DnsResolver(dns.resolver.Resolver):
	"""
	DNS stub resolver.
	"""

	def __init__(self, **kwargs):
		self.udp_socks = []
		self.tcp_socks = []
		super().__init__(**kwargs)

	def query(self, qname, qtype='A', qclass='IN',   use_cache=True):
		"""
		Query upstream server or local cache for response to DNS query.
		"""

		# Convert arguments to correct datatypes
		if isinstance(qname, str):
			qname = dns.name.from_text(qname, None)

		# Create list of names to query for
		qnames = []
		if qname.is_absolute():
			qnames.append(qname)
		else:
			if len(qname) > 1:
				qnames.append(qname.concatenate(dns.name.root))
			if self.search:
				for suffix in self.search:
					qnames.append(qname.concatenate(self.domain))

		all_nxdomain = True
		nxdomains = {}
		start = time.time()

		# Try all names and exit on first successful response
		for name in qnames:
			# Search local cache
			if self.cache and use_cache:
				answer = self.cache.get((name, qtype, qclass))

				if answer is not None:
					return answer

			# Prepare DNS query for upstream server
			request = dns.message.make_query(name, qtype, qclass)

			response = None

			nameservers = self.nameservers[:]
			errors = []

			# Rotate upstream server list if necessary
			if self.rotate:
				random.shuffle(nameservers)
				backoff = 0.10

			# Keep trying until acceptable answer
			while response is None:
				if len(nameservers) == 0:
					pass

				# Try all nameservers
				for nameserver in nameservers[:]:
					timeout = self._compute_timeout(start)
					port = self.nameserver_ports.get(nameserver, self.port)

					try:
						tcp_attempt = False

						if tcp_attempt:
							response = tcp_forward(None, (nameserver, port), request, timeout)
						else:
							response = udp_forward(None, (nameserver, port), request, timeout)

							if response.flags & dns.flags.TC:
								tcp_attempt = True
								timeout = self._compute_timeout(start)
								response = tcp_forward(None, (nameserver, port), request, timeout)

					# Socket or timeout error
					except (socket.error, dns.exception.Timeout) as exc:
						response = None
						continue

					# Received reply from wrong source
					except dns.query.UnexpectedSource as exc:
						response = None
						continue

					# Received malformed data
					except dns.exception.FormError as exc:
						nameservers.remove(nameserver)
						response = None
						continue

					# Using TCP but connection failed
					except EOFError as exc:
						nameservers.remove(nameserver)
						response = None
						continue

					rcode = response.rcode()

					if rcode == dns.rcode.YXDOMAIN:
						pass

					if rcode == dns.rcode.NOERROR or rcode == dns.rcode.NXDOMAIN:
						break

					if rcode != dns.rcode.SERVFAIL or not self.retry_servfail:
						nameservers.remove(nameserver)

					response = None

				if response is not None:
					break

				# All nameservers failed to respond ideally
				if len(nameservers) > 0:
					timeout = self._compute_timeout(start)
					sleep_time = min(timeout, backoff)
					backoff *= 2
					time.sleep(sleep_time)

			if response.rcode() == dns.rcode.NXDOMAIN:
				nxdomains[name] = response
				continue

			all_nxdomain = False
			break

		if all_nxdomain:
			response = nxdomains[qnames[0]]

		answer = dns.resolver.Answer(name, qtype, qclass, response, False)

		if self.cache:
			self.cache.put((name, qtype, qclass), answer)

		return answer

	def worker(self, timeout):
		"""
		Worker to monitor cache and perform upstream requests on expired entries.
		"""

		if self.cache is None:
			return

		while True:
			expired = self.cache.expired(timeout)

			for key in expired:
				logging.info('Updating %s' % (key[0]))
				self.query(*key, use_cache=False)

			time.sleep(timeout)


def upstream_resolve_b(resolver, packet):
	# Convert wireformat to dns message
	request = dns.message.from_wire(packet)
	query = request.question[0]

	# Get response from resolver
	try:
		response = resolver.query(query.name, query.rdtype, query.rdclass, raise_on_no_answer=False, ).response
	except dns.resolver.NXDOMAIN as exc:
		response = exc.response(query.name)

	response.id = request.id

	# Repack dns response to wireformat
	return response.to_wire()

async def udp_request(request, upstream, timeout):
	loop = asyncio.get_event_loop()

	sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, 0)
	sock.setblocking(0)

	await loop.sock_connect(sock, upstream)

	start = time.time()
	await loop.sock_sendall(sock, request)
	response = await loop.sock_recv(sock, 65535)

	if start is None:
		rtt = 0
	else:
		rtt = time.time() - start

	sock.close()

	return response

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
