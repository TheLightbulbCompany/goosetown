FROM barichello/godot-ci:4.6
COPY . /app
WORKDIR /app
EXPOSE 8080
CMD ["godot", "--headless", "--path", ".", "server_main.tscn"]
