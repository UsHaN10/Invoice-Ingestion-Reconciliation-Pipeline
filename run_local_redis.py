import sys
from fakeredis import TcpFakeServer

def main():
    host = "127.0.0.1"
    port = 6379
    print(f"Starting standalone Redis TCP server on {host}:{port}...")
    server = TcpFakeServer((host, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Redis server stopped.")

if __name__ == "__main__":
    main()
