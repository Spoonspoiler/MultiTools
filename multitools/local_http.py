"""HTTP direct pour les services privés, sans proxy ni suivi de redirection."""

from contextlib import contextmanager
from http.client import HTTPConnection, HTTPException
from ipaddress import ip_address, ip_network
import json
from socket import SHUT_RDWR
from threading import Event, Thread
from urllib.parse import urlsplit


_LAN = tuple(ip_network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


class LocalServiceError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


class HttpStatusError(LocalServiceError):
    def __init__(self, status: int, detail: str):
        self.status = status
        super().__init__(f"HTTP {status} · {detail}")


def check_cancel(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled("Tâche annulée.")


def private_host(value: str, *, lan: bool = False) -> str:
    value = value.strip()
    if value == "localhost":
        return "127.0.0.1"
    address = ip_address(value)
    if not address.is_loopback and not (lan and any(address in network for network in _LAN)):
        raise ValueError("Utilise une adresse de boucle locale ou une adresse IPv4 du réseau privé.")
    return str(address)


class LocalHTTP:
    def __init__(self, host: str, port: int, *, lan: bool = False):
        self.host = private_host(host, lan=lan)
        self.port = int(port)
        if not 1 <= self.port <= 65535:
            raise ValueError("Port HTTP invalide.")

    @property
    def base(self) -> str:
        address = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{address}:{self.port}"

    @contextmanager
    def open(self, route: str, data=None, *, cancel: Event | None = None, timeout: float = 10):
        parsed = urlsplit(route)
        if not route.startswith("/") or parsed.netloc or parsed.scheme or route.startswith("//"):
            raise ValueError("Chemin HTTP local invalide.")
        check_cancel(cancel)
        con = HTTPConnection(self.host, self.port, timeout=min(3, timeout))
        end = Event()
        try:
            con.connect()
            sock = con.sock
            sock.settimeout(timeout)
            if cancel is not None:
                def interrupt():
                    while not end.wait(0.12):
                        if cancel.is_set():
                            try:
                                sock.shutdown(SHUT_RDWR)
                            except OSError:
                                pass
                            return
                Thread(target=interrupt, daemon=True).start()
            body = None if data is None else json.dumps(data).encode()
            headers = {"Content-Type": "application/json"} if body is not None else {}
            con.request("GET" if data is None else "POST", route, body=body, headers=headers)
            with con.getresponse() as response:
                if not 200 <= response.status < 300:
                    detail = response.reason if response.status < 400 else response.read(4096).decode("utf-8", "replace")
                    raise HttpStatusError(response.status, detail[:350] or response.reason)
                yield response
            check_cancel(cancel)
        except (OSError, HTTPException) as error:
            check_cancel(cancel)
            raise LocalServiceError(f"Service inaccessible sur {self.base} · {error}") from error
        finally:
            end.set()
            con.close()

    def read(self, route: str, data=None, *, cancel=None, timeout=10, limit=2 * 1024 * 1024) -> bytes:
        with self.open(route, data, cancel=cancel, timeout=timeout) as response:
            body = response.read(limit + 1)
            if len(body) > limit:
                raise LocalServiceError("Réponse du service trop volumineuse.")
            return body

    def json(self, route: str, data=None, **kwargs) -> dict:
        try:
            value = json.loads(self.read(route, data, **kwargs))
        except (ValueError, UnicodeError) as error:
            raise LocalServiceError("Réponse JSON du service invalide.") from error
        if not isinstance(value, dict):
            raise LocalServiceError("Le service a renvoyé un résultat inattendu.")
        return value
