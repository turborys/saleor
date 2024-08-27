import graphene
from django.core.exceptions import ValidationError

from .....core.tracing import traced_atomic_transaction
from .....permission.enums import ProductPermissions
from .....product import models
from .....product.error_codes import ProductErrorCode
from ....channel import ChannelContext
from ....core import ResolveInfo
from ....core.doc_category import DOC_CATEGORY_PRODUCTS
from ....core.mutations import BaseMutation
from ....core.types import ProductError
from ....plugins.dataloaders import get_plugin_manager_promise
from ...types import ProductMedia, ProductVariant


class VariantMediaAssign(BaseMutation):
    product_variant = graphene.Field(ProductVariant)
    media = graphene.Field(ProductMedia)

    class Arguments:
        media_id = graphene.ID(
            required=True, description="ID of a product media to assign to a variant."
        )
        variant_id = graphene.ID(required=True, description="ID of a product variant.")

    class Meta:
        description = "Assign an media to a product variant."
        doc_category = DOC_CATEGORY_PRODUCTS
        permissions = (ProductPermissions.MANAGE_PRODUCTS,)
        error_type_class = ProductError
        error_type_field = "product_errors"

    @classmethod
    def perform_mutation(  # type: ignore[override]
        cls, _root, info: ResolveInfo, /, *, media_id, variant_id
    ):
        media = cls.get_node_or_error(
            info, media_id, field="media_id", only_type=ProductMedia
        )
        qs = models.ProductVariant.objects.all()
        variant = cls.get_node_or_error(
            info, variant_id, field="variant_id", only_type=ProductVariant, qs=qs
        )
        with traced_atomic_transaction():
            if media and variant:
                # check if the given image and variant can be matched together
                media_belongs_to_product = variant.product.media.filter(
                    pk=media.pk
                ).first()
                if media_belongs_to_product:
                    _, created = media.variant_media.get_or_create(variant=variant)
                    if not created:
                        raise ValidationError(
                            {
                                "media_id": ValidationError(
                                    "This media is already assigned",
                                    code=ProductErrorCode.MEDIA_ALREADY_ASSIGNED.value,
                                )
                            }
                        )
                else:
                    raise ValidationError(
                        {
                            "media_id": ValidationError(
                                "This media doesn't belong to that product.",
                                code=ProductErrorCode.NOT_PRODUCTS_IMAGE.value,
                            )
                        }
                    )
            variant = ChannelContext(node=variant, channel_slug=None)
            manager = get_plugin_manager_promise(info.context).get()
            cls.call_event(manager.product_variant_updated, variant.node)
        return VariantMediaAssign(product_variant=variant, media=media)


class VariantMediaBulkAssign(BaseMutation):
    product_variants = graphene.List(graphene.NonNull(ProductVariant))
    media_list = graphene.List(graphene.NonNull(ProductMedia))

    class Arguments:
        media_alts = graphene.List(
            graphene.String, required=True,
            description="List of alt texts of product media to assign to variants."
        )
        skus = graphene.List(
            graphene.String, required=True, description="List of product variant SKUs."
        )

    class Meta:
        description = "Assign multiple media (by alt text) to multiple product variants (by SKU)."
        doc_category = DOC_CATEGORY_PRODUCTS
        permissions = (ProductPermissions.MANAGE_PRODUCTS,)
        error_type_class = ProductError
        error_type_field = "product_errors"

    @classmethod
    def perform_mutation(
        cls, _root, info: ResolveInfo, /, *, media_alts, skus
    ):
        # Проверка на соответствие количества media_alts и skus
        if len(media_alts) != len(skus):
            raise ValidationError(
                {
                    "media_alts": ValidationError(
                        "The number of media alt texts must match the number of SKUs.",
                        code=ProductErrorCode.INVALID.value,
                    )
                }
            )

        media_list = []
        product_variants = []
        with traced_atomic_transaction():
            for media_alt, sku in zip(media_alts, skus):
                variant = models.ProductVariant.objects.filter(sku=sku).first()
                if not variant:
                    raise ValidationError(
                        {
                            "skus": ValidationError(
                                f"No product variant found with SKU '{sku}'.",
                                code=ProductErrorCode.NOT_FOUND.value,
                            )
                        }
                    )

                media = variant.product.media.filter(alt=media_alt).first()
                if not media:
                    raise ValidationError(
                        {
                            "media_alts": ValidationError(
                                f"No media found with alt text '{media_alt}' for this product.",
                                code=ProductErrorCode.NOT_FOUND.value,
                            )
                        }
                    )

                if media:
                    variant_media, created = media.variant_media.get_or_create(
                        variant=variant
                    )
                    if not created:
                        variant_media.save()

                    media_list.append(media)
                    product_variants.append(variant)
                else:
                    raise ValidationError(
                        {
                            "media_alts": ValidationError(
                                "This media doesn't belong to that product.",
                                code=ProductErrorCode.NOT_PRODUCTS_IMAGE.value,
                            )
                        }
                    )

            manager = get_plugin_manager_promise(info.context).get()
            for variant in product_variants:
                variant = ChannelContext(node=variant, channel_slug=None)
                cls.call_event(manager.product_variant_updated, variant.node)

        return VariantMediaBulkAssign(
            product_variants=product_variants, media_list=media_list)
