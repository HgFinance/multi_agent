# Production image for the AI Office frontend.
# The build is intentionally isolated from the Python services: the browser
# reaches the canonical portfolio-bff through the same-origin /bff proxy.
FROM node:22-bookworm-slim AS build

WORKDIR /app/ai-office

COPY ai-office/package.json ai-office/package-lock.json ./
RUN npm ci

COPY ai-office ./
RUN npm run build

FROM node:22-bookworm-slim AS runtime

ENV NODE_ENV=production
WORKDIR /app/ai-office

COPY --from=build /app/ai-office/package.json ./package.json
COPY --from=build /app/ai-office/package-lock.json ./package-lock.json
COPY --from=build /app/ai-office/node_modules ./node_modules
COPY --from=build /app/ai-office/dist ./dist

EXPOSE 3000

CMD ["./node_modules/.bin/vinext", "start", "--host", "0.0.0.0", "--port", "3000"]
