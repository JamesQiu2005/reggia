FROM node:24-alpine

# Sandbox dependencies (bubblewrap for filesystem isolation, socat for network)
RUN apk add --no-cache bubblewrap socat curl

RUN npm install -g @anthropic-ai/claude-code

WORKDIR /workspace

COPY backend/chat_workspace/ /workspace/
COPY docker-entrypoint.sh /
RUN chmod +x /docker-entrypoint.sh

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["sleep", "infinity"]
