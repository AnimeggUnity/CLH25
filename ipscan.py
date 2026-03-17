import subprocess, concurrent.futures, socket

SUBNET = "192.168.50"
TIMEOUT = 0.5

def ping(ip):
    result = subprocess.run(
        ["ping", "-n", "1", "-w", "300", ip],
        capture_output=True, text=True
    )
    if "TTL=" in result.stdout:
        # 嘗試反解主機名
        try:
            hostname = socket.gethostbyaddr(ip)[0]
        except:
            hostname = ""
        # 嘗試抓開放的常見 port
        ports = {}
        for port, name in [(80,"HTTP"),(443,"HTTPS"),(502,"Modbus"),(8080,"HTTP-ALT"),(22,"SSH"),(23,"Telnet")]:
            try:
                s = socket.socket()
                s.settimeout(TIMEOUT)
                s.connect((ip, port))
                s.close()
                ports[name] = port
            except:
                pass
        return ip, hostname, ports
    return None

print(f"Scanning {SUBNET}.1 ~ {SUBNET}.254 ...")
results = []

with concurrent.futures.ThreadPoolExecutor(max_workers=50) as ex:
    futures = {ex.submit(ping, f"{SUBNET}.{i}"): i for i in range(1, 255)}
    for f in concurrent.futures.as_completed(futures):
        r = f.result()
        if r:
            results.append(r)

results.sort(key=lambda x: int(x[0].split(".")[-1]))

print(f"\nFound {len(results)} hosts:\n")
for ip, hostname, ports in results:
    port_str = ", ".join(f"{n}({p})" for n, p in ports.items()) if ports else "no open ports"
    host_str = f" [{hostname}]" if hostname else ""
    marker = " <-- Modbus Master?" if "Modbus" in ports else ""
    print(f"  {ip}{host_str}  {port_str}{marker}")
