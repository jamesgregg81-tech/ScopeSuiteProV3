def scope_model_from_ident(ident):
    first = (ident or "").split(";")[0].strip().upper()
    return first.replace("FLUKE", "").strip() or "UNKNOWN"


def screen_capture_mode_for_ident(ident):
    model = scope_model_from_ident(ident)
    color_png_models = ("196C", "199C")
    b_series_models = ("196B", "199B")
    png_supported = any(token in model for token in color_png_models)
    if any(token in model for token in b_series_models):
        return "legacy", png_supported, model
    if png_supported:
        return "png", png_supported, model
    return "legacy", False, model


def screen_capture_filename_prefix(ident):
    model = scope_model_from_ident(ident).lower()
    if model and model != "unknown":
        return f"fluke{model}_screen"
    return "fluke_screen"
