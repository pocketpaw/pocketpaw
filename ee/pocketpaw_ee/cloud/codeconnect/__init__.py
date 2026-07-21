# codeconnect — Code Mode GitHub connect flow (CM-3).
# The durable "this user installed the GitHub App" binding + the install-URL /
# callback / repo-listing orchestration that turns it into a repo picker. The
# 4-file entity (domain/dto/service/router) persists the connection; connect.py
# is the GitHub-touching orchestration layer above the service (mirrors how
# codeproject/lifecycle.py sits above codeproject/service.py).
