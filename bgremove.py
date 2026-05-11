from PIL import (
    Image,
    ImageFilter,
    ImageEnhance,
    ImageOps
)

from rembg import (
    remove,
    new_session
)

# ==========================================
# ADVANCED AI MODEL
# ==========================================

# BEST MODEL FOR:
# - hair
# - clothes
# - face edges
# - passport photos

session = new_session(
    "isnet-general-use"
)

# ==========================================
# SMART EDGE FIX
# ==========================================

def refine_alpha(alpha):

    # smooth jagged edges

    alpha = alpha.filter(
        ImageFilter.GaussianBlur(1)
    )

    # remove tiny noise

    alpha = alpha.filter(
        ImageFilter.MedianFilter(3)
    )

    # stronger edges

    alpha = ImageEnhance.Contrast(
        alpha
    ).enhance(1.25)

    return alpha

# ==========================================
# COLOR DECONTAMINATION
# ==========================================

def remove_white_outline(img):

    bg = Image.new(
        "RGBA",
        img.size,
        (0, 0, 0, 0)
    )

    img = Image.alpha_composite(
        bg,
        img
    )

    return img

# ==========================================
# MAIN REMOVE FUNCTION
# ==========================================

def remove_background(
    input_path,
    output_path
):

    # OPEN

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
    ).enhance(1.2)

    image = ImageEnhance.Contrast(
        image
    ).enhance(1.08)

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

    output = output.convert(
        "RGBA"
    )

    # ======================================
    # REFINE MASK
    # ======================================

    r, g, b, a = output.split()

    a = refine_alpha(a)

    output.putalpha(a)

    # ======================================
    # REMOVE HALO
    # ======================================

    output = remove_white_outline(
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
