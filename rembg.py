from PIL import (
    Image,
    ImageFilter,
    ImageEnhance,
    ImageChops
)

from rembg import (
    remove,
    new_session
)

# ==========================================
# ADVANCED AI MODEL
# ==========================================

# BEST BALANCE:
# Accurate hair
# Better clothes protection
# Better edges

session = new_session(
    "isnet-general-use"
)

# ==========================================
# REMOVE BG
# ==========================================

def remove_background(
    input_path,
    output_path
):

    # OPEN IMAGE

    image = Image.open(
        input_path
    ).convert(
        "RGBA"
    )

    # ======================================
    # PRE ENHANCE
    # ======================================

    image = ImageEnhance.Sharpness(
        image
    ).enhance(1.15)

    image = ImageEnhance.Contrast(
        image
    ).enhance(1.05)

    # ======================================
    # AI REMOVE
    # ======================================

    output = remove(

        image,

        session=session,

        alpha_matting=True,

        alpha_matting_foreground_threshold=240,

        alpha_matting_background_threshold=10,

        alpha_matting_erode_size=8
    )

    # ======================================
    # CLEAN EDGES
    # ======================================

    output = output.convert(
        "RGBA"
    )

    r, g, b, a = output.split()

    # SMOOTH MASK

    a = a.filter(
        ImageFilter.GaussianBlur(0.7)
    )

    # IMPROVE EDGE QUALITY

    a = ImageEnhance.Contrast(
        a
    ).enhance(1.2)

    # REMOVE SMALL NOISE

    a = a.filter(
        ImageFilter.MedianFilter(3)
    )

    # APPLY FINAL ALPHA

    output.putalpha(a)

    # ======================================
    # ANTI WHITE HALO
    # ======================================

    background = Image.new(
        "RGBA",
        output.size,
        (0, 0, 0, 0)
    )

    output = Image.alpha_composite(
        background,
        output
    )

    # ======================================
    # SAVE
    # ======================================

    output.save(
        output_path,
        format="PNG"
    )

    return output_path
