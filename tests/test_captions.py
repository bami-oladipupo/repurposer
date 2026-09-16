from repurposer import captions


def wf(**over):
    base = {"title_template": "{caption_first_line}", "caption_template": "{caption}\n\n{yt_hashtags}", "hashtags": ["#Shorts"]}
    base.update(over)
    return base


def test_title_truncated_to_100_chars():
    long_line = "word " * 40  # 200 chars once stripped
    title, _ = captions.youtube_snippet({"tiktok_id": "1", "tiktok_caption": long_line}, wf())
    assert len(title) == captions.YT_TITLE_MAX == 100
    assert title.endswith("…")


def test_short_title_untouched_and_angle_brackets_removed():
    title, _ = captions.youtube_snippet({"tiktok_id": "1", "tiktok_caption": "Hello <world>\nsecond line"}, wf())
    assert title == "Hello world"


def test_shorts_appended_to_description_when_absent():
    video = {"tiktok_id": "1", "tiktok_caption": "Plain caption"}
    _, desc = captions.youtube_snippet(video, wf(hashtags=[]))
    assert "#shorts" not in desc.lower()
    _, desc = captions.youtube_snippet(video, wf(hashtags=["#Shorts"], caption_template="{caption}"))
    assert desc.endswith("#Shorts")
    assert desc.lower().count("#shorts") == 1


def test_shorts_not_duplicated_when_present():
    video = {"tiktok_id": "1", "tiktok_caption": "Already tagged #shorts"}
    title, desc = captions.youtube_snippet(video, wf(caption_template="{caption}"))
    assert (title + desc).lower().count("#shorts") == 2  # once in title, once in the templated description
    _, desc2 = captions.youtube_snippet({"tiktok_id": "1", "tiktok_caption": "x"}, wf())
    assert desc2.lower().count("#shorts") == 1  # template already includes {yt_hashtags}


def test_override_precedence_overrides_then_column_then_template():
    video = {"tiktok_id": "1", "tiktok_caption": "Template title\nbody", "yt_title": "Column title",
             "yt_description": "Column description"}
    t, d = captions.youtube_snippet(video, wf())
    assert (t, d.split("\n")[0]) == ("Column title", "Column description")
    t, d = captions.youtube_snippet(video, wf(), {"yt_title": "Override title", "yt_description": "Override description"})
    assert (t, d.split("\n")[0]) == ("Override title", "Override description")
    video.pop("yt_title"); video.pop("yt_description")
    t, d = captions.youtube_snippet(video, wf())
    assert t == "Template title"
    assert d.startswith("Template title\nbody")


def test_empty_caption_falls_back_to_tiktok_id_title():
    t, _ = captions.youtube_snippet({"tiktok_id": "999", "tiktok_caption": ""}, wf())
    assert t == "TikTok 999"


def test_instagram_caption_template_and_precedence():
    ig = {"caption_template": "{caption}\n.\n.\n{ig_hashtags}", "hashtags": ["#ba", "#career"]}
    video = {"tiktok_id": "1", "tiktok_caption": "Hello IG"}
    assert captions.instagram_caption(video, ig) == "Hello IG\n.\n.\n#ba #career"
    video["ig_caption"] = "Column caption"
    assert captions.instagram_caption(video, ig) == "Column caption"
    assert captions.instagram_caption(video, ig, {"ig_caption": "Override caption"}) == "Override caption"


def test_instagram_caption_truncated():
    video = {"tiktok_id": "1", "tiktok_caption": "x" * 3000}
    assert len(captions.instagram_caption(video, {"caption_template": "{caption}", "hashtags": []})) == captions.IG_CAPTION_MAX


def test_matches_exclusion_case_insensitive():
    kws = ["#ad", "paid partnership", "Kaplan"]
    assert captions.matches_exclusion("This is an #AD for shoes", kws) == "#ad"
    assert captions.matches_exclusion("PAID PARTNERSHIP with brand", kws) == "paid partnership"
    assert captions.matches_exclusion("brought to you by kaplan", kws) == "Kaplan"
    assert captions.matches_exclusion("nothing sponsored here", kws) is None
    assert captions.matches_exclusion(None, kws) is None
    assert captions.matches_exclusion("#ad", []) is None


def test_template_helpers():
    assert captions.first_line("  first \n second") == "first"
    assert captions.strip_hashtags("Hello #one world #two") == "Hello world"
    assert captions._fmt("{caption} {unknown}", {"caption": "c"}) == "c {unknown}"
