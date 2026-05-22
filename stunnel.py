import paramiko
import threading
import socket
import time

def forward_stream(src, dst):
    """
    通用的单向数据转发函数。
    一旦任意一端触发异常或断开，确保将两端的 socket/channel 全部关闭，
    以此来中断另一个方向的阻塞 recv。
    """
    try:
        while True:
            data = src.recv(4096)
            if not data: 
                break
            dst.sendall(data)
    except (socket.error, paramiko.SSHException, OSError): 
        pass
    finally:
        try:
            src.close()
        except: pass
        try:
            dst.close()
        except: pass

class SSHTunnel():
    def __init__(self, host_config):
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy()) 
        
        self.hostname = host_config['hostname']
        self.username = host_config['username']
        self.remote_port = host_config['remote_port']
        self.local_port = host_config['local_port']
        self.sshd_port = host_config.get('sshd_port', 22)
        self.target_host = host_config.get('target_host', '127.0.0.1')

        # 动态绑定配置参数的连接匿名函数
        if 'pkey' in host_config:
            self.connect_fn = lambda: self.ssh.connect(hostname=self.hostname, username=self.username, port=self.sshd_port, timeout=30, pkey=host_config['pkey'])
        elif 'key_filename' in host_config:
            self.connect_fn = lambda: self.ssh.connect(hostname=self.hostname, username=self.username, port=self.sshd_port, timeout=30, key_filename=host_config['key_filename'])       
        else:
            self.connect_fn = lambda: self.ssh.connect(hostname=self.hostname, username=self.username, port=self.sshd_port, timeout=30, password=host_config.get('password'))
            
        self.running = False
        self.local_server = None

    def start_tunn(self):
        self.running = True
        
        self.local_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.local_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.local_server.bind(('127.0.0.1', self.local_port))
        self.local_server.listen(10)
        # 【关键改进】设置超时，防止 accept 永远阻塞导致无法退出
        self.local_server.settimeout(1.0) 

        while self.running:
            try:
                # 【关键改进】重连前先清理老连接，避免句柄泄漏
                self.ssh.close() 
                
                print(f"Connecting to SSH server {self.hostname}:{self.sshd_port}...")
                self.connect_fn()
                transport = self.ssh.get_transport()
                if transport:
                    transport.set_keepalive(30)

                print(f"SSH Tunnel started. Listening on local port {self.local_port} -> {self.target_host}:{self.remote_port}")

                while self.running:
                    try:
                        local_client_socket, addr = self.local_server.accept()
                    except socket.timeout:
                        # accept 超时，回到循环顶部检查 self.running 状态
                        continue
                    except (socket.error, OSError):
                        # 如果 local_server 被关闭，会进入这里
                        break

                    try:
                        if transport is None or not transport.is_active():
                            raise paramiko.SSHException("SSH Transport is inactive.")
                        
                        ssh_channel = transport.open_channel(
                            kind='direct-tcpip',
                            dest_addr=(self.target_host, self.remote_port),
                            src_addr=addr
                        )
                        
                        if ssh_channel is None:
                            print('Cannot create SSH channel.')
                            local_client_socket.close()
                            continue
                        
                        # 【关键改进】使用统一转发逻辑，保证两端生死同步
                        t1 = threading.Thread(target=forward_stream, args=(local_client_socket, ssh_channel), daemon=True)
                        t2 = threading.Thread(target=forward_stream, args=(ssh_channel, local_client_socket), daemon=True)
                        t1.start()
                        t2.start()
                    except Exception as e:
                        print(f'Error creating ssh_channel: {e}')
                        local_client_socket.close()

            except (paramiko.SSHException, socket.error) as error:
                if not self.running:
                    break
                print(f'Connection Loss: {error}. Retrying in 10 seconds...')
                # 用睡眠代替复杂的 Event，每 1 秒检查一次 running 状态，响应更快
                for _ in range(10):
                    if not self.running:
                        break
                    time.sleep(1)
            except KeyboardInterrupt:
                break
        
        # 统一清理
        self.cleanup()

    def cleanup(self):
        self.running = False
        try:
            if self.local_server:
                self.local_server.close()
        except: pass
        try:
            self.ssh.close()
        except: pass
        print('SSH Tunnel Exit Successfully.')

    def stop(self):
        print("Stopping SSH Tunnel...")
        self.running = False
        # 关闭 local_server 会立即使 accept() 抛出异常，从而打破阻塞
        if self.local_server:
            try:
                self.local_server.close()
            except: pass