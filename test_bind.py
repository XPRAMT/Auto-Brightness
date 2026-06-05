import socket
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(('127.0.0.1', 57321))
    print('BOUND')
    s.close()
except Exception as e:
    print('ERROR', type(e).__name__, e)
