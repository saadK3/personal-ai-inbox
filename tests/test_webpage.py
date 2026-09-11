from app.services.webpage import (
    MAX_DESCRIPTION_LENGTH,
    MAX_HEADINGS,
    canonicalize_url,
    extract_metadata_from_html,
    extract_url,
)


def test_extract_url_and_canonicalize_identity() -> None:
    text = "Read this: https://Example.com/article/?utm_source=chat#section."
    url = extract_url(text)
    assert url == "https://Example.com/article/?utm_source=chat#section"
    assert canonicalize_url(url) == "https://example.com/article?utm_source=chat"


def test_extract_metadata_is_bounded_and_ignores_body_and_scripts() -> None:
    html = """
    <html>
      <head>
        <title>Fallback title</title>
        <meta property="og:title" content="Useful article title">
        <meta name="author" content="Ada Example">
        <meta property="article:published_time" content="2026-09-10">
        <meta name="description" content="A useful description for later retrieval.">
        <script type="application/ld+json">
          {
            "headline":"Schema headline","author":{"name":"Schema Author"},
            "datePublished":"2026-09-09"
          }
        </script>
      </head>
      <body>
        <h1>First heading</h1>
        <h2>Second heading</h2>
        <p>BODY_ONLY_TEXT_SHOULD_NOT_BE_STORED</p>
        <script>SECRET_SCRIPT_TEXT_SHOULD_NOT_BE_STORED</script>
      </body>
    </html>
    """

    metadata = extract_metadata_from_html("https://example.com/article", html)

    assert metadata.title == "Useful article title"
    assert metadata.author == "Ada Example"
    assert metadata.published_date == "2026-09-10"
    assert metadata.description == "A useful description for later retrieval."
    assert metadata.headings == ["First heading", "Second heading"]
    assert metadata.extraction_status == "complete"
    serialized = str(metadata.as_dict())
    assert "BODY_ONLY_TEXT_SHOULD_NOT_BE_STORED" not in serialized
    assert "SECRET_SCRIPT_TEXT_SHOULD_NOT_BE_STORED" not in serialized


def test_extract_metadata_caps_description_and_heading_count() -> None:
    headings = "".join(f"<h2>Heading {index}</h2>" for index in range(MAX_HEADINGS + 5))
    metadata = extract_metadata_from_html(
        "https://example.com/article",
        f'<meta name="description" content="{"x" * (MAX_DESCRIPTION_LENGTH + 100)}">{headings}',
    )

    assert len(metadata.description or "") == MAX_DESCRIPTION_LENGTH
    assert len(metadata.headings) == MAX_HEADINGS
