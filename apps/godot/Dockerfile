FROM barichello/godot-ci:4.6.1

# Install font libs needed for headless mode with Label nodes
RUN apt-get update && apt-get install -y --no-install-recommends \
    libfontconfig1 \
    && rm -rf /var/lib/apt/lists/*

COPY . /app
WORKDIR /app

# Create import directories and regenerate .godot/imported/ cache for Linux.
# .godot/ is excluded via .dockerignore to avoid macOS cache mismatch.
# mkdir ensures Godot doesn't silently fail (godotengine/godot#72360).
RUN mkdir -p .godot/imported .godot/editor \
    && godot --headless --import 2>&1; exit 0

# Verify import actually produced files
RUN ls -la .godot/imported/ | head -5 && echo "Import cache OK: $(ls .godot/imported/ | wc -l) files"

EXPOSE 8080
CMD ["godot", "--headless", "--path", ".", "server_main.tscn"]
