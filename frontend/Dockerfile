# OPUS UI (DIDA pattern). Two shipping shapes off one dependency install:
#   * dev  — `vite dev` (HMR, source bind-mounted by compose). The dev box opts in.
#   * prod — a real `vite build` (adapter-node) served by the package's server.mjs,
#            which also proxies /api → the backend. Nothing bind-mounted matters: the build
#            is baked in.
# compose selects the stage via the OPUS_UI_TARGET build arg (default: prod).
FROM node:24-trixie-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# --- dev: Vite dev server, source comes from the compose bind mount ------------
FROM node:24-trixie-slim AS dev
ENV NODE_ENV=development
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . ./
RUN npx svelte-kit sync
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]

# --- build: produce the production bundle (adapter-node → /app/build) ----------
FROM node:24-trixie-slim AS build
ENV NODE_ENV=production
WORKDIR /app
# where the other modules answer. Vite reads these at build time, so a module
# that is not deployed simply has no address and stays out of the switcher.
ARG VITE_OPUS_LIBRARY_URL
ARG VITE_OPUS_PLAYER_URL
COPY --from=deps /app/node_modules ./node_modules
COPY . ./
# the tsconfig extends what sync generates, and the build reads the tsconfig
RUN npx svelte-kit sync && npm run build

# --- prod: thin runtime serving the baked build + /api proxy (the package's server.mjs)
FROM node:24-trixie-slim AS prod
ENV NODE_ENV=production
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY --from=build /app/build ./build
COPY package.json ./package.json
COPY src/lib/opus/server.mjs ./server.mjs
RUN chown -R node:node /app
USER node
EXPOSE 5173
CMD ["node", "server.mjs"]
