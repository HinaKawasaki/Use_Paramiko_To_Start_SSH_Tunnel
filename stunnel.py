import paramiko
import threading
import socket
import time

def local_to_remote(local_socket, ssh_channel):
    try:
        while True:
            data = local_socket.recv(4096)
            if not data: break
            ssh_channel.sendall(data)
    except(socket.error,paramiko.SSHException,EOFError,BrokenPipeError): 
        pass
    finally:
        local_socket.close()
        ssh_channel.close()

def remote_to_local(local_socket, ssh_channel):
    try:
        while True:
            data = ssh_channel.recv(4096)
            if not data: break
            local_socket.sendall(data)
    except(socket.error,paramiko.SSHException,EOFError,BrokenPipeError): 
        pass

class SSHTunnel():
    def __init__(self,host_config):
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy()) # 上线应用的时候应该使用系统的known_hosts
        self.hostname = host_config['hostname']
        self.username = host_config['username']
        self.remote_port = host_config['remote_port']
        self.local_port = host_config['local_port']
        if 'sshd_port' in host_config:
            sshd_port = host_config['sshd_port']
        else:
            sshd_port = 22
        if 'target_host' in host_config:
            self.target_host = host_config['target_host']
        else:
            self.target_host = '127.0.0.1'

        if 'pkey' in host_config:
            login = 0
            self.pkey = host_config['pkey']         
            self.connect = lambda: self.ssh.connect(hostname=self.hostname,username=self.username,port=sshd_port,timeout=30,pkey=self.pkey)
        elif 'key_filename' in host_config:
            login = 1
            self.key_filename = host_config['key_filename']
            self.connect = lambda: self.ssh.connect(hostname=self.hostname,username=self.username,port=sshd_port,timeout=30,key_filename=self.key_filename)       
        else:
            login = 2
            self.password = host_config['password']
            self.connect = lambda:self.ssh.connect(hostname=self.hostname,username=self.username,port=sshd_port,timeout=30,password=self.password)
        self.login = login
        self.running = True

    def start_tunn(self):

        self.local_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.local_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.local_server.bind(('127.0.0.1',self.local_port))
        self.local_server.listen(10)
        while self.running:
            try:
                self.connect()
                transport = self.ssh.get_transport()
                transport.set_keepalive(30)

                while self.running:
                    local_client_socket,addr = self.local_server.accept()
                    try:
                        if not transport.is_active():
                            raise paramiko.SSHException()
                        ssh_channel = transport.open_channel(
                            kind='direct-tcpip',
                            dest_addr=(self.target_host,self.remote_port),
                            src_addr=addr)
                        if ssh_channel is None:
                            print('Can not create a SshTunnel while doing: ssh_channel=transport.open_channel.')
                            local_client_socket.close()
                            continue
                        t1 = threading.Thread(target=local_to_remote, args=(local_client_socket, ssh_channel), daemon=True)
                        t2 = threading.Thread(target=remote_to_local, args=(local_client_socket, ssh_channel), daemon=True)
                        t1.start()
                        t2.start()
                    except:
                        print('Error to create a ssh_channel.')
                        local_client_socket.close()

            except (paramiko.SSHException,socket.error, Exception) as error:
                print(f'Connection Loss:{error}')
                time.sleep(10)
                continue
            except KeyboardInterrupt:
                self.ssh.close()
                self.local_server.close()
                print('Exit!')
                
    def stop(self):
        self.running = False
        self.ssh.close()
        self.local_server.close()
        print('SSH Tunnel Exit Successfully.')
