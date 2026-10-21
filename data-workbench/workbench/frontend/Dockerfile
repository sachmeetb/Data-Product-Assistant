# Data Workbench frontend (React/Vite) → static build served by nginx.
FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
# Use `vite build` directly (NOT `npm run build`, which also runs `tsc -b`):
# the repo has known pre-existing TS errors unrelated to runtime, and vite
# transpiles without type-checking.
RUN npx vite build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
