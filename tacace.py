import socket

UDP_IP = "0.0.0.0"
UDP_PORT = 514

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((UDP_IP, UDP_PORT))

print("Listening for syslog...")

while True:
    data, addr = sock.recvfrom(4096)
    print(f"From {addr}: {data.decode()}")
