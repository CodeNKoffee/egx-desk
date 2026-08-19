# Drop TradingView CSV exports here.
#
# TradingView has real EGX coverage but NO data API at any subscription tier, and
# its terms prohibit automated collection outright ("scripts, APIs, screen
# scraping, data mining, robots... regardless of their intended purposes"), with
# market data licensed display-only. Community MCP servers that wrap its internal
# websocket exist and get accounts banned.
#
# What it does offer on Plus/Premium is an official export:
#   open the chart -> three dots (top right) -> Export chart data -> CSV
#
# A human clicking that button is not automation. Filenames like BIOC.csv,
# BIOC.CA.csv and TradingView's own "EGX_BIOC, 1D.csv" all resolve.
