import graphene
from django.core.exceptions import ValidationError
from django.core.files import File

from .....core.http_client import HTTPClient
from .....core.utils.validators import get_oembed_data
from .....permission.enums import ProductPermissions
from .....product import ProductMediaTypes, models
from .....product.error_codes import ProductErrorCode
from .....thumbnail.utils import get_filename_from_url
from ....channel import ChannelContext
from ....core import ResolveInfo
from ....core.doc_category import DOC_CATEGORY_PRODUCTS
from ....core.mutations import BaseMutation
from ....core.types import BaseInputObjectType, ProductError, Upload
from ....core.validators.file import clean_image_file, is_image_url, validate_image_url
from ....plugins.dataloaders import get_plugin_manager_promise
from ...types import Product, ProductMedia
from ...utils import ALT_CHAR_LIMIT


class ProductMediaCreateInput(BaseInputObjectType):
    alt = graphene.String(description="Alt text for a product media.")
    image = Upload(
        required=False, description="Represents an image file in a multipart request."
    )
    product = graphene.ID(
        required=True, description="ID of an product.", name="product"
    )
    media_url = graphene.String(
        required=False, description="Represents an URL to an external media."
    )

    class Meta:
        doc_category = DOC_CATEGORY_PRODUCTS


class ProductMediaCreate(BaseMutation):
    product = graphene.Field(Product)
    media = graphene.Field(ProductMedia)

    class Arguments:
        input = ProductMediaCreateInput(
            required=True, description="Fields required to create a product media."
        )

    class Meta:
        description = (
            "Create a media object (image or video URL) associated with product. "
            "For image, this mutation must be sent as a `multipart` request. "
            "More detailed specs of the upload format can be found here: "
            "https://github.com/jaydenseric/graphql-multipart-request-spec"
        )
        doc_category = DOC_CATEGORY_PRODUCTS
        permissions = (ProductPermissions.MANAGE_PRODUCTS,)
        error_type_class = ProductError
        error_type_field = "product_errors"

    @classmethod
    def validate_input(cls, data):
        image = data.get("image")
        media_url = data.get("media_url")
        alt = data.get("alt")

        if not image and not media_url:
            raise ValidationError(
                {
                    "input": ValidationError(
                        "Image or external URL is required.",
                        code=ProductErrorCode.REQUIRED.value,
                    )
                }
            )
        if image and media_url:
            raise ValidationError(
                {
                    "input": ValidationError(
                        "Either image or external URL is required.",
                        code=ProductErrorCode.DUPLICATED_INPUT_ITEM.value,
                    )
                }
            )

        if alt and len(alt) > ALT_CHAR_LIMIT:
            raise ValidationError(
                {
                    "input": ValidationError(
                        f"Alt field exceeds the character "
                        f"limit of {ALT_CHAR_LIMIT}.",
                        code=ProductErrorCode.INVALID.value,
                    )
                }
            )

    @classmethod
    def perform_mutation(  # type: ignore[override]
        cls, _root, info: ResolveInfo, /, *, input
    ):
        cls.validate_input(input)
        product = cls.get_node_or_error(
            info,
            input["product"],
            field="product",
            only_type=Product,
            qs=models.Product.objects.all(),
        )

        alt = input.get("alt", "")
        media_url = input.get("media_url")
        media = None
        if img_data := input.get("image"):
            input["image"] = info.context.FILES.get(img_data)
            image_data = clean_image_file(input, "image", ProductErrorCode)
            media = product.media.create(
                image=image_data, alt=alt, type=ProductMediaTypes.IMAGE
            )
        if media_url:
            # Remote URLs can point to the images or oembed data.
            if is_image_url(media_url):
                validate_image_url(
                    media_url, "media_url", ProductErrorCode.INVALID.value
                )
                filename = get_filename_from_url(media_url, include_hash=False)
                image_data = HTTPClient.send_request(
                    "GET", media_url, stream=True, allow_redirects=False
                )
                image_file = File(image_data.raw, filename)
                existing_media = models.ProductMedia.objects.filter(
                    image=f"products/{image_file}").first()

                if existing_media:
                    existing_media.alt = alt
                    existing_media.type = ProductMediaTypes.IMAGE
                    existing_media.save()
                    media = existing_media
                else:
                    media = product.media.create(
                        image=image_file,
                        alt=alt,
                        type=ProductMediaTypes.IMAGE,
                    )
            else:
                oembed_data, media_type = get_oembed_data(media_url, "media_url")

                existing_media = product.media.filter(
                    external_url=oembed_data["url"]).first()

                if existing_media:
                    existing_media.alt = oembed_data.get("title", alt)
                    existing_media.type = media_type
                    existing_media.oembed_data = oembed_data
                    existing_media.save()
                    media = existing_media
                else:
                    media = product.media.create(
                        external_url=oembed_data["url"],
                        alt=oembed_data.get("title", alt),
                        type=media_type,
                        oembed_data=oembed_data,
                    )

        manager = get_plugin_manager_promise(info.context).get()
        cls.call_event(manager.product_updated, product)
        cls.call_event(manager.product_media_created, media)
        product = ChannelContext(node=product, channel_slug=None)
        return ProductMediaCreate(product=product, media=media)
