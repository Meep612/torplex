FROM alpine:3.19

RUN apk add --no-cache tor

COPY torrc /etc/tor/torrc

EXPOSE 9050

USER tor

CMD ["tor", "-f", "/etc/tor/torrc"]
