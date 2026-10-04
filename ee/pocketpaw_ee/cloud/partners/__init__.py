# Paw Partners: a workspace that resells sites to its own clients. Partner-side
# reads/writes (profile, clients, offers, sales, earnings, tiers) in ``service``;
# public cross-tenant reads (directory, ``/partners/{slug}``), the public
# application and the operator's application queue in ``service_admin``; the
# operator set/clear switch and the review routes live in
# ``cloud/platform/partners.py``.
