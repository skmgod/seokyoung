"""운영용 실행: 같은 사무실(네트워크)의 다른 PC에서도 접속할 수 있게 띄운다."""
import socket
import sys

from waitress import serve

from app import app

port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
try:
    ip = socket.gethostbyname(socket.gethostname())
except OSError:
    ip = "이 PC의 IP"
print(f"주택관리 프로그램 실행 중 - 이 PC: http://127.0.0.1:{port}  /  다른 PC: http://{ip}:{port}")
print("종료하려면 이 창을 닫으세요.")
serve(app, host="0.0.0.0", port=port)
