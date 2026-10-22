# Golden version-2 page

`page.html` was rendered once by verbatim-index's own `annotations_page.render_page`
at verbatim commit `d5a3a8bf`, from the synthetic `data.json` here, with:

```sh
python3.12 -I tests/fixtures/golden_v2/make_golden.py <verbatim>/scripts tests/fixtures/golden_v2
```

`tests/test_page.py` renders the same data with this package and requires the
bytes to match. The check proves the port is exact. It is retired when the page
template changes on purpose (plan Phase 2).
