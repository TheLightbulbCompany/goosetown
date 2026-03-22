FROM barichello/godot-ci:4.6.1

# Install missing font/rendering libs needed for headless mode with Label nodes
RUN apt-get update && apt-get install -y --no-install-recommends \
    libfontconfig1 \
    && rm -rf /var/lib/apt/lists/*

COPY . /app
WORKDIR /app
EXPOSE 8080
CMD ["godot", "--headless", "--path", ".", "server_main.tscn"]
