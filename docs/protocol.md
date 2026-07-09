# Wire Protocol

The host and the sandbox never share memory, a socket, or a process — they rendezvous only through the
durable [`Conduit`](api/conduit.md). The contract below is **language-neutral**: any runtime that can
read and write JSON and gzip-tar archives to the conduit can act as a host or a guest. The Python
package in this repo is one reference implementation of it.

The full specification, kept in `spec/PROTOCOL.md` in the repository and embedded verbatim here:

--8<-- "spec/PROTOCOL.md"
