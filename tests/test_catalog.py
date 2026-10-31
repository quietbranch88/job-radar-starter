from pathlib import Path

from job_radar.catalog import load_catalog


def test_public_catalog_separates_sources_from_user_preferences(tmp_path: Path):
    catalog_file = tmp_path / "sources.yaml"
    catalog_file.write_text(
        """
countries:
  CA:
    name: Canada
    sources: [greenhouse, lever]
  SG:
    name: Singapore
    sources: [ashby]
sources:
  greenhouse:
    kind: official_api
    terms_url: https://developer.greenhouse.io/job-board.html
    enabled: true
companies:
  Shopify:
    countries: [CA]
    source: greenhouse
    board: shopify
""".strip(),
        encoding="utf-8",
    )

    catalog = load_catalog(catalog_file)

    assert catalog.countries["CA"].sources == ("greenhouse", "lever")
    assert catalog.countries["SG"].name == "Singapore"
    assert catalog.sources["greenhouse"].kind == "official_api"
    assert catalog.sources["greenhouse"].enabled is True
    assert catalog.companies["Shopify"].board == "shopify"
    assert catalog.companies["Shopify"].countries == ("CA",)


def test_shipped_catalog_lists_the_private_editions_public_boards():
    catalog = load_catalog(Path(__file__).parents[1] / "catalog" / "sources.yaml")
    expected = {
        "Airwallex": ("ashby", "airwallex", ("SG", "US")),
        "Cohere": ("ashby", "cohere", ("CA", "US")),
        "Faire": ("greenhouse", "faire", ("US", "CA")),
        "GitLab": ("greenhouse", "gitlab", ("REMOTE",)),
        "Monzo": ("greenhouse", "monzo", ("GB",)),
        "OpenAI": ("ashby", "openai", ("US", "SG", "JP", "GB")),
        "PayPay": ("greenhouse", "paypay", ("JP",)),
    }

    assert set(catalog.companies) == set(expected)
    assert catalog.countries["US"].name == "United States"
    for name, (source, board, countries) in expected.items():
        company = catalog.companies[name]
        assert company.source == source
        assert company.board == board
        assert company.countries == countries
        assert not company.board.startswith("replace-with-your-board")
