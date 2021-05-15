#!/usr/bin/env python3
import argparse
import asyncio
import base64
import logging
import random
from abc import ABCMeta, abstractmethod
from types import TracebackType
from typing import Iterable, Optional, Tuple, Type

import aiohttp

DEFAULT_LISTEN_ADDRESSES = \
(
	'127.0.0.1',
	'::1',
)

DEFAULT_LISTEN_PORTS = \
(
	5053,
)

DEFAULT_UPSTREAMS = \
(
	'https://1.1.1.1:443/dns-query',
	'https://1.0.0.1:443/dns-query',
	'https://[2606:4700:4700::1111]/dns-query',
	'https://[2606:4700:4700::1001]/dns-query',
)


async def main() -> None:
	# Handle command line arguments
	parser = argparse.ArgumentParser()
	parser.add_argument('-l', '--listen-address', nargs='+', default=DEFAULT_LISTEN_ADDRESSES,
						help='addresses to listen on for DNS over HTTPS requests (default: %(default)s)')
	parser.add_argument('-p', '--listen-port', nargs='+', type=int, default=DEFAULT_LISTEN_PORTS,
						help='ports to listen on for DNS over HTTPS requests (default: %(default)s)')
	parser.add_argument('-u', '--upstreams', nargs='+', default=DEFAULT_UPSTREAMS,
						help='upstream servers to forward DNS queries and requests to (default: %(default)s)')
	parser.add_argument('-t', '--tcp', action='store_true', default=False,
						help='serve TCP based queries and requests along with UDP (default: %(default)s)')
	args = parser.parse_args()

	# Setup logging
	logging.basicConfig(level='INFO', format='[%(levelname)s] %(message)s')
	logging.info('Starting DNS over HTTPS forwarder')
	logging.info('Args: %r' % (vars(args)))

	# Setup event loop
	loop = asyncio.get_running_loop()

	# Setup DNS resolver to cache/forward queries and answers
	async with AsyncDnsResolver(AsyncDohUpstreamContext(upstream) for upstream in args.upstreams) as resolver:
		# Setup listening transports
		transports = []
		for addr in args.listen_address:
			for port in args.listen_port:
				# Setup UDP server
				logging.info('Starting UDP server listening on %s#%d' % (addr, port))
				udp, _ = await loop.create_datagram_endpoint(lambda: UdpResolverProtocol(resolver), local_addr=(addr, port))
				transports.append(udp)

				# Setup TCP server
				if args.tcp:
					logging.info('Starting TCP server listening on %s#%d' % (addr, port))
					tcp = await asyncio.start_server(lambda r, w: handle_tcp_peer(r, w, resolver), addr, port)
					transports.append(tcp)

		# Serve forever
		try:
			while True:
				await asyncio.sleep(5)

		except (KeyboardInterrupt, SystemExit):
			pass

		logging.info('Shutting down DNS over HTTPS forwarder')

		wait_closers = []
		for transport in transports:
			transport.close()
			if hasattr(transport, 'wait_closed'):
				wait_closers.append(asyncio.create_task(transport.wait_closed()))

		await asyncio.wait(wait_closers)

	await asyncio.sleep(0.3)


class AsyncDnsUpstreamContext(metaclass=ABCMeta):
	"""A base class used to manage upstream DNS server connections and metadata."""

	def __init__(self, address: str) -> None:
		self.address = address
		self.rtt = 0.0
		self.queries = 0
		self.answers = 0

	async def __aenter__(self) -> 'AsyncDnsUpstreamContext':
		return self

	async def __aexit(self,
		exc_type: Optional[Type[BaseException]],
		exc_val: Optional[BaseException],
		exc_tb: Optional[TracebackType]) -> None:
		await self.close()

	def get_stats(self) -> str:
		"""Returns a formatted string of statistics for this upstream server."""
		return f'{self.address} (rtt: {self.rtt:.3f} s, queries: {self.queries}, answers: {self.answers})'

	@abstractmethod
	async def forward_query(self, query: bytes) -> bytes:
		"""Resolve a DNS query via forwarding to a upstream DNS server.

		Params:
			query - A wireformat DNS request packet.

		Returns:
			A wireformat DNS response packet.
		"""
		...

	@abstractmethod
	async def close(self) -> None:
		"""Close any open connections to the upstream DNS server."""
		...


class AsyncDohUpstreamContext(AsyncDnsUpstreamContext):
	"""A class used to manage upstream DoH server connections and metadata."""

	def __init__(self, url: str) -> None:
		super().__init__(url)
		self.session = aiohttp.ClientSession()

	async def forward_post(self, query: bytes) -> bytes:
		"""Resolve a DNS query via forwarding to a upstream DoH server (POST).

		Params:
			query - A wireformat DNS request packet.

		Returns:
			A wireformat DNS response packet.

		Notes:
			Using DNS over HTTPS POST format as described here:
			https://tools.ietf.org/html/draft-ietf-doh-dns-over-https-12
			https://developers.cloudflare.com/1.1.1.1/dns-over-https/wireformat/
		"""
		loop = asyncio.get_running_loop()

		headers = {'accept': 'application/dns-message', 'content-type': 'application/dns-message'}
		rtt = loop.time()
		self.queries += 1

		async with self.session.post(self.address, headers=headers, data=query) as http:
			# Log abnormal HTTP status codes
			if http.status != 200:
				self.rtt += 1.0
				raise ConnectionError(f'received HTTP error status from DoH Server {self.address} ({http.status})')

			# Wait for response
			answer = await http.read()
			rtt = loop.time() - rtt
			self.answers += 1

			# Update estimated RTT for this upstream connection
			self.rtt = 0.875 * self.rtt + 0.125 * rtt

			return answer

	async def forward_get(self, query: bytes) -> bytes:
		"""Resolve a DNS query via forwarding to a upstream DoH server (GET).

		Params:
			query - A wireformat DNS request packet.

		Returns:
			A wireformat DNS response packet.

		Notes:
			Using DNS over HTTPS GET format as described here:
			https://tools.ietf.org/html/draft-ietf-doh-dns-over-https-12
			https://developers.cloudflare.com/1.1.1.1/dns-over-https/wireformat/
		"""
		loop = asyncio.get_running_loop()

		# Encode DNS query into url
		headers = {'accept': 'application/dns-message'}
		url = ''.join([self.address, '?dns=', base64.urlsafe_b64encode(query).decode()])
		rtt = loop.time()
		self.queries += 1

		async with self.session.get(url, headers=headers) as http:
			# Log abnormal HTTP status codes
			if http.status != 200:
				self.rtt += 1.0
				raise ConnectionError(f'received HTTP error status from DoH Server {self.address} ({http.status})')

			# Wait for response
			answer = await http.read()
			rtt = loop.time() - rtt
			self.answers += 1

			# Update estimated RTT for this upstream connection
			self.rtt = 0.875 * self.rtt + 0.125 * rtt

			return answer

	forward_query = forward_post

	async def close(self) -> None:
		await self.session.close()


class AsyncDnsResolver:
	"""A class that manages upstream DNS server contexts and resolves DNS queries."""

	def __init__(self, upstreams: Iterable[AsyncDnsUpstreamContext]) -> None:
		self._upstreams = tuple(upstreams)

	async def __aenter__(self) -> 'AsyncDnsResolver':
		return self

	async def __aexit__(self,
		exc_type: Optional[Type[BaseException]],
		exc_val: Optional[BaseException],
		exc_tb: Optional[TracebackType]) -> None:
		await self.close()

	def _select_upstream_rtt(self) -> AsyncDnsUpstreamContext:
		"""Selects a upstream DNS server to forward to (biased towards upstreams with a lower rtt)."""
		rtts = [upstream.rtt for upstream in self._upstreams]
		max_rtt = max(rtts)
		weights = (max_rtt - rtt + 1.0 for rtt in rtts)
		return random.choices(self._upstreams, weights=weights)[0]

	@property
	def queries(self) -> int:
		return sum([upstream.queries for upstream in self._upstreams])

	@property
	def answers(self) -> int:
		return sum([upstream.answers for upstream in self._upstreams])

	@property
	def avg_rtt(self) -> float:
		return sum([upstream.rtt for upstream in self._upstreams]) / len(self._upstreams)

	def get_stats(self) -> str:
		"""Returns a formatted string of statistics for this resolver."""
		return f'Statistics for resolver at 0x{id(self)} (avg_rtt: {self.avg_rtt:.3f} s, total_queries: {self.queries}, total_answers: {self.answers})'

	async def resolve(self, query: bytes, timeout: float = 3.0) -> bytes:
		"""Resolve a DNS query via forwarding to a upstream DNS server.

		Params:
			query - A wireformat DNS request packet.
			timeout - The maximum wait time (in seconds) for the DNS response packet.

		Returns:
			A wireformat DNS response packet.
		"""

		# Select upstream to forward to
		upstream = self._select_upstream_rtt()

		# Forward request upstream
		try:
			# Return response
			return await asyncio.wait_for(upstream.forward_query(query), timeout)

		except asyncio.TimeoutError as exc:
			raise TimeoutError('DNS request expired and was cancelled') from exc

		except Exception as exc:
			upstream.rtt += 2.0
			raise exc

		# Reset RTT every 1000 processed requests to prevent drift
		finally:
			if self.queries % 1000 == 0:
				logging.info(self.get_stats())
				for upstream in self._upstreams:
					logging.info(upstream.get_stats())
					upstream.rtt = 0.0

	async def close(self) -> None:
		"""Close all upstream DoH server connections."""
		for upstream in self._upstreams:
			await upstream.close()


class UdpResolverProtocol(asyncio.DatagramProtocol):
	"""Protocol for serving UDP DNS requests via a DnsResolver instance."""

	def __init__(self, resolver: AsyncDnsResolver):
		self.resolver = resolver

	def connection_made(self, transport: asyncio.DatagramTransport) -> None:
		self.transport = transport

	def datagram_received(self, data: bytes, peer: Tuple[str, int]) -> None:
		# Schedule packet processing coroutine
		asyncio.create_task(self.process_packet(peer, data))

	async def process_packet(self, peer: Tuple[str, int], query: bytes) -> None:
		# Resolve DNS query
		answer = await self.resolver.resolve(query)

		# Send DNS answer to client
		self.transport.sendto(answer, peer)


async def handle_tcp_peer(
	reader: asyncio.StreamReader,
	writer: asyncio.StreamWriter,
	resolver: AsyncDnsResolver) -> None:
	"""Coroutine for serving TCP DNS requests via a DnsResolver instance."""
	tasks = []
	wlock = asyncio.Lock()
	while True:
		# Check if our peer has finished writing to the stream
		if reader.at_eof():
			break

		# Parse a DNS query packet off of the wire
		query_size = int.from_bytes(await reader.readexactly(2), 'big')
		query = await reader.readexactly(query_size)

		# Schedule the processing of the query
		tasks.append(asyncio.create_task(handle_tcp_query(writer, wlock, resolver, query)))

	# Wait for all scheduled query processing to finish
	await asyncio.wait(tasks)

	# Indicate we are done writing to the stream
	if writer.can_write_eof():
		writer.write_eof()

	# Close the stream
	writer.close()
	await writer.wait_closed()

async def handle_tcp_query(
	writer: asyncio.StreamWriter,
	wlock: asyncio.Lock,
	resolver: AsyncDnsResolver,
	query: bytes) -> None:
	"""Coroutine for serving TCP DNS responses via a DnsResolver instance."""
	# Resolve DNS query
	answer = await resolver.resolve(query)
	answer = b''.join([len(answer).to_bytes(2, 'big'), answer])

	# Write the DNS answer to the stream
	async with wlock:
		writer.write(answer)
		await writer.drain()


if __name__ == '__main__':
	asyncio.run(main())
