# Static hosting for the standalone Accenture Data Products Portal.
# index.html is fully self-contained (React, Tailwind, Babel, Lucide loaded via
# CDN), so no build step is required — nginx simply serves it.
FROM nginx:1.27-alpine

RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html /usr/share/nginx/html/index.html

EXPOSE 8080
CMD ["/bin/sh", "-c", "printf 'window.__PORTAL_CONFIG__ = { bronzeUrl: \"%s\", silverUrl: \"%s\", goldUrl: \"%s\" };\n' \"${BRONZE_URL:-}\" \"${SILVER_URL:-}\" \"${GOLD_URL:-}\" > /usr/share/nginx/html/env-config.js && exec nginx -g 'daemon off;'"]
