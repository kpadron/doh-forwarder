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
args = parser.parse_args()

host = args.listen_address
port = args.listen_port
upstreams = args.upstreams
cache_size = args.max_cache_size

# resolver = dns.resolver.Resolver(configure=False)
# resolver = DnsResolver(configure=False)
resolver = None
# resolver.nameservers = upstreams

# Basic diagram
#           Q           Q
# listener -> cache {} -> forwarder
#           Q           Q

# Queue for listener to post requests
cache_request = janus.Queue()

# Queue for cache to post responses
cache_response = janus.Queue()

# Queue for cache to store outstanding requests
cache_outstanding = {}

# Queue for cache to post requests
forward_request = janus.Queue()

# Queue for forwarder to post responses
forward_response = janus.Queue()

def main():
	# Setup logging
	logging.basicConfig(level='INFO', format='[%(levelname)s] %(message)s')

	# Setup resolver cache
	global resolver
	resolver = DnsResolver(configure=False)
	resolver.nameservers = upstreams
	resolver.cache = DnsLruCache(cache_size)
	resolver.thread = threading.Thread(target=resolver.worker, args=(10,), daemon=True)
	# resolver.cache = dns.resolver.LRUCache(cache_size)

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
		resolver.thread.start()
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
	DNS over UDP protocol.
	"""

	def connection_made(self, transport):
		self.transport = transport

	def datagram_received(self, data, addr):
		asyncio.ensure_future(self.resolve(data, addr))

	def error_received(self, exc):
		logging.warning('Minor transport error')

	async def resolve_packet(self, query, addr):
		# Post query to cache -> (query, addr)
		await cache_request.async_q.put((query, addr))

		# Get response from cache <- (response, addr)
		response, addr = await cache_response.async_q.get()

		# Send DNS packet to client
		self.transport.sendto(response, addr)

def cache_worker():
	while True:
		query, addr = cache_request.sync_q.get_nowait()
		request = dns.message.from_wire(query)

		cache_outstanding[(request.qname, request.qtype, request.qclass)].append((request.id, addr))

		


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


class DnsLruCache(dns.resolver.LRUCache):
	"""
	DNS record cache.
	"""

	def get(self, key):
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

	# def __len__(self):
	# 	with self.lock:
	# 		return len(self.data)
	#
	# def __iter__(self):
	# 	return self.keys()
	#
	# def keys(self):
	# 	with self.lock:
	# 		return self.data.keys()
	#
	# def values(self):
	# 	with self.lock:
	# 		return self.data.values()
	#
	# def items(self):
	# 	with self.lock:
	# 		return self.data.items()


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


def upstream_resolve(resolver, packet):
	"""
	Respond to a DNS request using resolver.

	Params:
		resolver - resolver object to use when resolving requests
		packet - raw bytes wireformat DNS request

	Returns:
		A raw bytes wireformat DNS response.

	Notes:
		Only supports requests with 1 question.
	"""

	# Convert wireformat to dns message
	request = dns.message.from_wire(packet)
	query = request.question[0]

	# Get response from resolver
	try:
		response = resolver.query(query.name, query.rdtype, query.rdclass).response
	except dns.resolver.NXDOMAIN as exc:
		response = exc.response(query.name)

	response.id = request.id

	# Repack dns response to wireformat
	return response.to_wire()


if __name__ == '__main__':
	main()
