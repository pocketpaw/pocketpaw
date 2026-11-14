# ee/pocketpaw_ee/site_ai: the AI API a published Paw Site calls through us.
#
# Sites send OpenAI-shaped chat requests naming a model ALIAS; we run them on our
# provider keys through LiteLLM and bill the owner's workspace wallet, bounded by
# the site's hard caps. See router.py for the routes and the gate order.
