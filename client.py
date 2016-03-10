import os
import json
import threading
import uuid
from functools import partial

import tkinter as tk
from tkinter.filedialog import askopenfilename
from tkinter.simpledialog import askstring
from tkinter.messagebox import askquestion
from tkinter import scrolledtext

from tornado import tcpclient, tcpserver, ioloop, iostream, gen


HOST, PORT = "localhost", 15180
DOWNLOAD_HOST, DOWNLOAD_PORT = "localhost", 18100
FILE_CHUNK_SIZE = 1024


class NicknameNotAvailableException(Exception):
    pass


class TornadoThread(threading.Thread):
    def run(self):
        self.loop = ioloop.IOLoop.current()
        self.loop.start()

    def stop(self):
        self.loop.stop()


class ChatClient(tcpclient.TCPClient):
    def __init__(self, *args, **kwargs):
        super(ChatClient, self).__init__(*args, **kwargs)
        self.connected = False
        self.files = {}

    @gen.coroutine
    def connect(self, nickname):
        """
        Tries to connect to the server, returns True if connects,
        raises NicknameNotAvailableException,
        if there is a user with such nick.
        """
        try:
            self.stream = yield super(ChatClient, self).connect(HOST, PORT)
        except iostream.StreamClosedError:
            self.on_message("The server is hanging out, come later.")
            return False
        yield self.send_data(nickname, message_type="login_attempt")
        received = yield self.receive()

        if received["content"] == "fail":
            raise NicknameNotAvailableException

        self.connected = True
        self.nickname = nickname
        return True

    def disconnect(self):
        self.stream.close()
        self.connected = False

    @gen.coroutine
    def receive(self, stream=None):
        """
        Receives messages from stream, gets message
        length by number before "|" separator.
        :param stream: Stream to get the message from,
        if not provided defaults to self.stream.
        """
        if not stream:
            stream = self.stream
        data_size = yield stream.read_until(b"|")
        data_size = int(data_size[:-1])
        data = yield stream.read_bytes(data_size, partial=True)
        return json.loads(data.decode("utf8"))

    @gen.coroutine
    def receive_loop(self, callback=None):
        """
        Infinite loop for receiving messages, if callback is
        given, it will be called with received message argument.
        """
        while True:
            try:
                received = yield self.receive()
            except iostream.StreamClosedError:
                return
            if received["type"] == "message":
                self.on_message(received["content"])
            elif received["type"] == "room_update":
                self.on_room_update(received["content"])
            elif received["type"] == "file_link":
                self.on_file_link(received["content"])
            elif received["type"] == "file_request":
                receiver_host, receiver_port, file_id = received["content"]
                self.send_file(file_id=file_id,
                               to=(receiver_host, receiver_port))
            if callback:
                callback(received)

    def on_message(self, message):
        raise NotImplemented("You should provide on_message handler")

    def on_room_update(self, rooms):
        raise NotImplemented("You should provide on_room_update handler")

    def on_file_link(self, file_info):
        raise NotImplemented("You should provide on_file_link handler")

    @gen.coroutine
    def send_data(self, content, message_type="message", stream=None):
        """
        Encodes given message and sends it to the stream, if stream
        argument is given, will use the one in the argument,
        self.stream otherwise.
        """
        if not stream:
            stream = self.stream
        data = json.dumps({"content": content,
                           "type": message_type}).encode("utf8")
        data = "{}|".format(len(data)).encode("utf8") + data
        stream.write(data)

    def create_room(self, room_name):
        self.send_data(room_name, message_type="create_room")

    def enter_room(self, room_name):
        self.send_data(room_name, message_type="enter_room")

    def create_peer_link(self, file_path):
        """
        Maps local filepath to uuid and sends uuid to the server,
        for creating a download link.
        """
        if not os.path.exists(file_path):
            raise OSError("Filepath {} does not exist, refusing "
                          "to make link.".format(file_path))
        file_id = uuid.uuid4().hex
        file_size = os.stat(file_path).st_size
        file_name = os.path.basename(file_path)
        self.files[file_id] = file_path
        self.send_data((file_id, file_name, file_size),
                       message_type="file_peer_link")

    @gen.coroutine
    def send_file(self, file_path=None, file_id=None, to=(HOST, PORT)):
        """
        Sends file given by file_path or file_id
        creating a new connection to given host and port.
        :param file_id: May be used instead of file_path,
        if its in the self.files dict.
        """
        if file_id:
            file_path = self.files[file_id]
        elif not file_path:
            raise Exception("You should provide file_path or file_id.")
        file_name = os.path.basename(file_path)
        try:
            file_size = os.stat(file_path).st_size
        except FileNotFoundError:
            status = "fail"
            file_size = 0
        else:
            status = "ok"
        receiver_host, receiver_port = to

        fileclient = tcpclient.TCPClient()
        filestream = yield fileclient.connect(receiver_host, receiver_port)
        yield self.send_data((status, self.nickname, file_name, file_size),
                             message_type="file_send", stream=filestream)
        if status == "ok":
            with open(file_path, 'rb') as f:
                sended = 0
                while sended < file_size:
                    yield filestream.write(f.read(FILE_CHUNK_SIZE))
                    sended += FILE_CHUNK_SIZE
        filestream.close()

    @gen.coroutine
    def download_file(self, file_id, file_name):
        """
        Creates a TCP server and sends request to download file.
        :param file_name: Name to save the file with.
        """

        class Downloader(tcpserver.TCPServer):
            chat_client = self

            @gen.coroutine
            def handle_stream(self, stream, address):
                stream.set_close_callback(self.stop)
                received = yield self.chat_client.receive(stream=stream)
                status, _, _, file_size = received["content"]
                if status == "ok":
                    with open(file_name, 'wb') as f:
                        received = 0
                        while received < file_size:
                            chunk = yield stream.read_bytes(FILE_CHUNK_SIZE,
                                                            partial=True)
                            f.write(chunk)
                            received += FILE_CHUNK_SIZE
                    self.chat_client.on_message("File {} was downloaded."
                                                .format(file_name))
                else:
                    self.chat_client.on_message("Unfortunately, the file {} "
                                                "is not currently "
                                                "available.".format(file_name))
                stream.close()

        downloader = Downloader()
        downloader.listen(DOWNLOAD_PORT, HOST)
        self.send_data((HOST, DOWNLOAD_PORT, file_id),
                       message_type="file_request")


class MainApplication(tk.Frame, ChatClient):
    def __init__(self, parent, *args, **kwargs):
        tk.Frame.__init__(self, parent, *args, **kwargs)
        ChatClient.__init__(self, *args, **kwargs)
        self.parent = parent

        self.parent.title("Chat")
        self.parent.geometry("400x300")

        self.text = tk.StringVar()
        self.name = tk.StringVar()
        self.name.set("")
        self.text.set("")

        self.log = scrolledtext.ScrolledText(self.parent)
        self.log.config(state=tk.DISABLED)
        self.log.tag_config("hyperlink", foreground="blue", underline=1)
        self.log.tag_bind("hyperlink", "<Button-1>", self.hyperlink_click)
        self.log.tag_config("hyperlink_peer", foreground="red", underline=1)
        self.log.tag_bind("hyperlink_peer", "<Button-1>", self.hyperlink_click)

        self.msg = tk.Entry(self.parent, textvariable=self.text)
        self.msg_label = tk.Label(self.parent, text=("Your message (click"
                                                     "<Return> to send it):"))
        self.msg.config(state=tk.DISABLED)

        self.nick = tk.Entry(self.parent, textvariable=self.name)
        self.nick_label = tk.Label(self.parent, text="Your nickname:")
        self.connect_button = tk.Button(self.parent, text="Connect",
                                        command=self.toggle_connect)
        self.file_button = tk.Button(self.parent, text="Choose file",
                                     command=self.load_file)
        self.file_button.config(state=tk.DISABLED)
        self.create_room_button = tk.Button(self.parent, text="Create room",
                                            command=self.create_room)
        self.create_room_button.config(state=tk.DISABLED)

        self.create_room_button.pack(side="bottom", fill="x", expand=True)
        self.file_button.pack(side="bottom", fill="x", expand=True)
        self.connect_button.pack(side="bottom", fill="x", expand=True)
        self.msg.pack(side='bottom', fill='x', expand=True)
        self.msg_label.pack(side="bottom")
        self.nick.pack(side='bottom', fill='x', expand=True)
        self.nick_label.pack(side="bottom")
        self.log.pack(side='top', fill='both', expand=True)

        self.msg.bind('<Return>', lambda ev: self.send_message())

        self.room_buttons = []

    def load_file(self):
        """
        Opens dialogs to choose a file and then to
        choose uploading options.
        """
        file_path = askopenfilename()
        if file_path:
            question = ("Do you want to upload your file to the server "
                        "(if no, others will download it from your computer)?")
            uploading = askquestion("Upload to server", question)
            if uploading == "yes":
                self.send_file(file_path)
            else:
                try:
                    self.create_peer_link(file_path)
                except OSError as err:
                    self.on_message(str(err))

    @gen.coroutine
    def send_message(self):
        """
        Sends the string in the Entry to the server as a message,
        and prints it to the user.
        """
        if self.text.get():
            yield super(MainApplication, self).send_data(self.text.get())
            self.print_message("<You>: {}".format(self.text.get()))
            self.text.set("")

    @gen.coroutine
    def toggle_connect(self):
        if not self.connected:
            if not self.name.get():
                self.print_message("Please, provide nickname.")
                return
            try:
                connected = yield self.connect(self.name.get())
            except NicknameNotAvailableException:
                self.print_message("Please, choose another nickname.")
                return

            if connected:
                self.connect_button.config(text="Disconnect")
                self.msg.config(state=tk.NORMAL)
                self.nick.config(state=tk.DISABLED)
                self.create_room_button.config(state=tk.NORMAL)
                self.file_button.config(state=tk.NORMAL)
                self.receive_future = self.receive_loop()
        else:
            self.disconnect()
            self.connect_button.config(text="Connect")
            self.msg.config(state=tk.DISABLED)
            self.nick.config(state=tk.NORMAL)
            self.create_room_button.config(state=tk.DISABLED)
            self.file_button.config(state=tk.DISABLED)
            self.receive_future.cancel()

    def on_message(self, message):
        self.print_message(message)

    def print_message(self, message, br=True, tags=[]):
        """
        :param br: Add new line after message.
        :param tags: List of tags to add to the message.
        """
        self.log.config(state=tk.NORMAL)
        pattern = "{}\n" if br else "{}"
        self.log.insert(tk.END, pattern.format(message), tags)
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

    def on_file_link(self, file_info):
        file_sender, file_id, file_name = file_info
        self.print_message("{}: ".format(file_sender), br=False)
        self.print_message(file_name, tags=["hyperlink", file_id, file_name])

    def hyperlink_click(self, event):
        file_id, file_name = event.widget.tag_names(tk.CURRENT)[1:]
        self.download_file(file_id, file_name)

    def on_room_update(self, rooms):
        """
        Updates room buttons.
        :param rooms: List of room names.
        """
        [button.destroy() for button in self.room_buttons]
        self.room_buttons = []
        for room in rooms:
            room_button = tk.Button(self.parent, text=room,
                                    command=partial(self.enter_room, room))
            room_button.pack(side="right")
            self.room_buttons.append(room_button)

    def create_room(self):
        """
        Shows dialog and creates a new room with provided name.
        """
        room_name = askstring("Create room", "New room name:")
        if room_name:
            super(MainApplication, self).create_room(room_name)


if __name__ == "__main__":
    tornado_thread = TornadoThread()
    tornado_thread.start()

    root = tk.Tk()
    app = MainApplication(root)
    app.pack(side="top", fill="both", expand=True)
    root.mainloop()
    tornado_thread.stop()
