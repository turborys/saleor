import graphene
from django.core.exceptions import ValidationError

from .....permission.enums import ProductPermissions
from .....product import ProductMediaTypes, models
from .....product.error_codes import ProductErrorCode
from ....channel import ChannelContext
from ....core import ResolveInfo
from ....core.doc_category import DOC_CATEGORY_PRODUCTS
from ....core.mutations import BaseMutation
from ....core.types import BaseInputObjectType, ProductError, Upload
from ....plugins.dataloaders import get_plugin_manager_promise
from ...types import Product, ProductMedia


class ProductMediaBulkCreateOrUpdateInput(BaseInputObjectType):
    alt = graphene.String(description="Alt text for a product media.")
    image = Upload(
        required=False, description="Represents an image file in a multipart request."
    )
    product = graphene.String(
        required=True, description="Slug of a product.", name="product"
    )
    media_url = graphene.String(
        required=False, description="Represents a URL to an external media."
    )

    class Meta:
        doc_category = DOC_CATEGORY_PRODUCTS


class ProductMediaBulkCreateOrUpdate(BaseMutation):
    product = graphene.Field(Product)
    media = graphene.List(graphene.NonNull(ProductMedia))

    class Arguments:
        products = graphene.List(
            graphene.NonNull(ProductMediaBulkCreateOrUpdateInput),
            required=True,
            description="List of products with media to create or update.",
        )

    class Meta:
        description = (
            "Bulk create or update media objects (image or video URL) associated with products. "
            "For image uploads, this mutation must be sent as a `multipart` request. "
            "More detailed specs of the upload format can be found here: "
            "https://github.com/jaydenseric/graphql-multipart-request-spec"
        )
        doc_category = DOC_CATEGORY_PRODUCTS
        permissions = (ProductPermissions.MANAGE_PRODUCTS,)
        error_type_class = ProductError
        error_type_field = "product_errors"

    @classmethod
    def perform_mutation(cls, _root, info: ResolveInfo, *, products):
        CHUNK_SIZE = 3000  # Define your chunk size
        media_to_create = []
        media_to_update = []
        product_slugs = [product_input["product"] for product_input in products]

        product_map = {p.slug: p for p in
                       models.Product.objects.filter(slug__in=product_slugs)}

        for product_input in products:
            product_slug = product_input["product"]
            product = product_map.get(product_slug)

            if not product:
                raise ValidationError(
                    {
                        "product": ValidationError(
                            f"Product with slug '{product_slug}' not found.",
                            code=ProductErrorCode.NOT_FOUND.value,
                        )
                    }
                )

            alt = product_input.get("alt", "")
            media_url = product_input.get("media_url")

            if media_url:
                existing_media = models.ProductMedia.objects.filter(
                    product=product, external_url=media_url
                ).first()

                if existing_media:
                    existing_media.alt = alt
                    existing_media.type = ProductMediaTypes.IMAGE
                    media_to_update.append(existing_media)
                else:
                    media_to_create.append(
                        models.ProductMedia(
                            product=product,
                            external_url=media_url,
                            alt=alt,
                            type=ProductMediaTypes.IMAGE,
                        )
                    )

        # Process media in chunks
        def bulk_create_chunks(media_list):
            for i in range(0, len(media_list), CHUNK_SIZE):
                chunk = media_list[i:i + CHUNK_SIZE]
                models.ProductMedia.objects.bulk_create(chunk)

        def bulk_update_chunks(media_list):
            for i in range(0, len(media_list), CHUNK_SIZE):
                chunk = media_list[i:i + CHUNK_SIZE]
                models.ProductMedia.objects.bulk_update(chunk, ["alt", "type"])

        if media_to_create:
            bulk_create_chunks(media_to_create)

        if media_to_update:
            bulk_update_chunks(media_to_update)

        media_results = media_to_create + media_to_update

        manager = get_plugin_manager_promise(info.context).get()
        cls.call_event(manager.product_updated, product)
        cls.call_event(manager.product_media_created, media_results)

        product = ChannelContext(node=product, channel_slug=None)
        return ProductMediaBulkCreateOrUpdate(product=product, media=media_results)
