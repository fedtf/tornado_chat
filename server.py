import os
import json
import uuid

from tornado import tcpserver, tcpclient, ioloop, iostream, gen


HOST = 'localhost'
PORT = 15180
FILE_CHUNK_SIZE = 1024


class ChatServer(tcpserver.TCPServer):
    clients = {}
    rooms = {"lobby": []}
    client_files = {}

    def __init__(self, *args, **kwargs):
        super(ChatServer, self).__init__(*args, **kwargs)
        try:
            os.mkdir('chat_files')
        except OSError:
            pass
        self.chat_files_path = os.path.abspath('chat_files')

    @gen.coroutine
    def handle_stream(self, stream, address):
        """
        Executes on new connection.
        :param stream: IOStream instance of a new connection.
        :param address: Host and port of a new client.
        """
        message = yield self.receive(stream)
        if message["type"] == "login_attempt":
            new_client_nickname = message["content"]
            if new_client_nickname not in self.clients:
                self.send("ok", client_stream=stream,
                          message_type="login_status")
            else:
                self.send("fail", client_stream=stream,
                          message_type="login_status")
                stream.close()
                return

            self.clients[new_client_nickname] = {"stream": stream,
                                                 "address": address}
            self.enter_room(new_client_nickname, "lobby")
            self.send(list(self.rooms.keys()), new_client_nickname,
                      message_type="room_update")

            self.receive_loop(new_client_nickname)
            stream.set_close_callback(lambda:
                                      self.on_close(new_client_nickname))
        elif message["type"] == "file_send":
            self.handle_file_upload(stream, message["content"])

    @gen.coroutine
    def receive(self, client_stream):
        """
        Receives message from provided IOStream instance.
        """
        message_size = yield client_stream.read_until(b"|")
        message_size = int(message_size[:-1])
        data = yield client_stream.read_bytes(message_size)
        return json.loads(data.decode("utf8"))

    @gen.coroutine
    def receive_loop(self, client_nickname):
        """
        Infinite loop for listening messages from the connection.
        :param client_nickname: Nickname saved in the self.clients dict.
        """
        client_stream = self.clients[client_nickname]["stream"]
        while True:
            try:
                message = yield self.receive(client_stream)
            except iostream.StreamClosedError:
                return
            if message["type"] == "message":
                self.broadcast("{}: {}".format(client_nickname,
                                               message["content"]),
                               client_nickname)
            elif message["type"] == "enter_room":
                self.leave_room(client_nickname,
                                self.clients[client_nickname]["room"])
                self.enter_room(client_nickname, message["content"])
            elif message["type"] == "create_room":
                self.create_room(client_nickname, message["content"])
            elif message["type"] == "file_request":
                self.handle_file_request(client_nickname, message["content"])
            elif message["type"] == "file_peer_link":
                self.broadcast_file_peer_link(client_nickname,
                                              message["content"])

    def broadcast_file_peer_link(self, client_nickname, file_info):
        """
        Saves info about the file provided by the client
        and broadcasts it to the room.
        :param client_nickname: Nickname of file's owner.
        :param file_info: Tuple with file's id, name and size.
        """
        file_id, file_name, file_size = file_info
        self.client_files[file_id] = client_nickname
        self.broadcast((client_nickname, file_id, file_name),
                       client_nickname, message_type="file_link")

    @gen.coroutine
    def handle_file_upload(self, upload_stream, file_info):
        """
        Saves the file uploaded by the client.
        :param upload_stream: Stream to get file from.
        :param file_info: Tuple with send_status, and
        file's owner nickname, name, size.
        """
        _, owner_nickname, file_name, file_size = file_info
        file_id = uuid.uuid4().hex
        file_path = os.path.join(self.chat_files_path, file_id)

        with open(file_path, 'wb') as f:
            received = 0
            while received < file_size:
                chunk = yield upload_stream.read_bytes(FILE_CHUNK_SIZE,
                                                       partial=True)
                f.write(chunk)
                received += FILE_CHUNK_SIZE
        upload_stream.close()
        self.io_loop.call_later(60*60*2, lambda: os.unlink(file_path))

        self.broadcast((owner_nickname, file_id, file_name),
                       owner_nickname, message_type="file_link")

    @gen.coroutine
    def handle_file_request(self, client_nickname, file_info):
        """
        Checks if the file with given id is on the server or on the client,
        sends request to file owner or sends a file to the requesting client.
        :param client_nickname: Nickname of the requsting client.
        :param file_info: Tuple with receiver host, receiver port and file's id
        """
        receiver_host, receiver_port, file_id = file_info
        if file_id in self.client_files:
            peer_nickname = self.client_files[file_id]
            self.send((receiver_host, receiver_port, file_id),
                      client_nickname=peer_nickname,
                      message_type="file_request")
        else:
            file_path = os.path.join(self.chat_files_path, file_id)
            try:
                file_size = os.stat(file_path).st_size
            except FileNotFoundError:
                status = "fail"
                file_size = 0
            else:
                status = "ok"

            fileclient = tcpclient.TCPClient()
            filestream = yield fileclient.connect(receiver_host, receiver_port)
            yield self.send((status, "server", "file", file_size),
                            client_stream=filestream, message_type="file_send")
            if status == "ok":
                with open(file_path, 'rb') as f:
                    sended = 0
                    while sended < file_size:
                        yield filestream.write(f.read(FILE_CHUNK_SIZE))
                        sended += FILE_CHUNK_SIZE
            filestream.close()

    def send(self, message,  client_nickname=None,
             client_stream=None, message_type="message"):
        """
        Encodes given message and sends it to the client.
        May be called with client's nickname or any IOStream instance.
        """
        data = json.dumps({"content": message,
                          "type": message_type}).encode("utf8")
        data = "{}|".format(len(data)).encode("utf8") + data
        if client_nickname:
            try:
                return self.clients[client_nickname]["stream"].write(data)
            except iostream.StreamClosedError:
                return
        elif client_stream:
            return client_stream.write(data)
        else:
            raise Exception("You should provide client's nickname or stream.")

    def broadcast(self, message, sender=None,
                  to="room", message_type="message"):
        """
        Sends given message to multiple receivers.
        :param sender: Client who sended a message, and doesn't receive it.
        :param to: May be "room" (default) or "all", in the latter
        case broadcasts to all online clients.
        """
        if to == "room":
            receivers = self.rooms[self.clients[sender]["room"]]
        elif to == "all":
            receivers = list(self.clients.keys())
        else:
            raise Exception('Unsupported "to" argument, '
                            'supported types: "room", "all".')
        for receiver_nickname in receivers:
            if receiver_nickname != sender:
                self.send(message, receiver_nickname,
                          message_type=message_type)

    def on_close(self, client_nickname):
        """
        Automatically executes when the client's stream is closed.
        """
        self.leave_room(client_nickname, self.clients[client_nickname]["room"])
        del self.clients[client_nickname]

    def create_room(self, client_nickname, room_name):
        """
        Creates a new room and makes given client to enter it.
        """
        self.rooms[room_name] = []
        self.leave_room(client_nickname, self.clients[client_nickname]["room"])
        self.enter_room(client_nickname, room_name)
        self.broadcast(list(self.rooms.keys()), to="all",
                       message_type="room_update")

    def leave_room(self, client_nickname, room_name):
        """
        Makes given client to leave the room,
        deletes the room if its empty and not lobby.
        """
        self.rooms[room_name].remove(client_nickname)
        self.broadcast("User {} has left the room.".format(client_nickname),
                       client_nickname)
        if room_name != "lobby" and len(self.rooms[room_name]) == 0:
            del self.rooms[room_name]
            self.broadcast(list(self.rooms.keys()), to="all",
                           message_type="room_update")

    def enter_room(self, client_nickname, room_name):
        room = self.rooms[room_name]
        if len(room) == 0:
            room_population_string = "you are alone here"
        elif len(room) == 1:
            room_population_string = "there is 1 user here: {}".format(room[0])
        else:
            room_population_string = ("there are {} users "
                                      "here: {}").format(len(room),
                                                         ", ".join(room))
        room.append(client_nickname)
        self.clients[client_nickname]["room"] = room_name
        self.broadcast("User {} has entered the room.".format(client_nickname),
                       client_nickname)
        self.send(("You have entered the "
                  "room {}, {}.").format(room_name, room_population_string),
                  client_nickname)

if __name__ == "__main__":
    server = ChatServer()
    server.listen(PORT)
    loop = ioloop.IOLoop.current()
    try:
        loop.start()
    except KeyboardInterrupt:
        loop.stop()
