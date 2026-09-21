# Static hosting for the standalone Accenture Data Products Portal.
# index.html is fully self-contained (React, Tailwind, Babel, Lucide loaded via
# CDN), so no build step is required — nginx simply serves it.
FROM nginx:1.27-alpine

RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html /usr/share/nginx/html/index.html

EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
