import graphene
from typing import List as TList, Dict, Any
from django.db.models import Q
from graphql import ResolveInfo
from graphene import ObjectType, List, Boolean, String

from .....attribute import models as attribute_models
from .....discount.utils import mark_active_catalogue_promotion_rules_as_dirty
from .....permission.enums import ProductPermissions
from .....product import models
from ....attribute.utils import AttrValuesInput, ProductAttributeAssignmentMixin
from ....core.descriptions import ADDED_IN_310
from ....core.mutations import ModelWithExtRefMutation
from ....core.types import ProductError
from ....plugins.dataloaders import get_plugin_manager_promise
from ...types import Product
from .product_create import ProductCreate, ProductInput

T_INPUT_MAP = list[tuple[attribute_models.Attribute, AttrValuesInput]]


class ProductUpdate(ProductCreate, ModelWithExtRefMutation):
    class Arguments:
        id = graphene.ID(required=False, description="ID of a product to update.")
        external_reference = graphene.String(
            required=False,
            description=f"External ID of a product to update. {ADDED_IN_310}",
        )
        input = ProductInput(
            required=True, description="Fields required to update a product."
        )

    class Meta:
        description = "Updates an existing product."
        model = models.Product
        object_type = Product
        permissions = (ProductPermissions.MANAGE_PRODUCTS,)
        error_type_class = ProductError
        error_type_field = "product_errors"
        support_meta_field = True
        support_private_meta_field = True

    @classmethod
    def clean_attributes(
        cls, attributes: dict, product_type: models.ProductType
    ) -> T_INPUT_MAP:
        attributes_qs = product_type.product_attributes.all()
        attributes = ProductAttributeAssignmentMixin.clean_input(
            attributes, attributes_qs, creation=False
        )
        return attributes

    @classmethod
    def post_save_action(cls, info: ResolveInfo, instance, cleaned_input):
        product = models.Product.objects.prefetched_for_webhook(single_object=True).get(
            pk=instance.pk
        )
        channel_ids = set(
            product.channel_listings.all().values_list("channel_id", flat=True)
        )
        cls.call_event(mark_active_catalogue_promotion_rules_as_dirty, channel_ids)

        manager = get_plugin_manager_promise(info.context).get()
        cls.call_event(manager.product_updated, product)

    @classmethod
    def get_instance(cls, info, **data):
        """Prefetch related fields that are needed to process the mutation."""
        # If we are updating an instance and want to update its attributes,
        # prefetch them.
        object_id = cls.get_object_id(**data)
        if object_id and data.get("attributes"):
            # Prefetches needed by ProductAttributeAssignmentMixin and
            # associate_attribute_values_to_instance
            qs = cls.Meta.model.objects.prefetch_related(
                "product_type__product_attributes__values",
                "product_type__attributeproduct",
            )
            return cls.get_node_or_error(info, object_id, only_type="Product", qs=qs)

        return super().get_instance(info, **data)


class ProductBulkUpdateResult(ObjectType):
    success = Boolean(description="Indicates if the bulk update was successful.")
    productErrors = List(
        ProductError, description="List of errors that occurred during the update.")


class ProductBulkUpdate(ProductCreate, ModelWithExtRefMutation):
    class Arguments:
        products = List(ProductInput, required=True,
                        description="List of products to update.")

    class Meta:
        description = "Updates multiple products."
        model = models.Product
        object_type = Product
        permissions = (ProductPermissions.MANAGE_PRODUCTS,)
        error_type_class = ProductError
        error_type_field = "product_errors"
        output = ProductBulkUpdateResult

    @classmethod
    def clean_attributes(
        cls, attributes: dict, product_type: models.ProductType
    ) -> T_INPUT_MAP:
        attributes_qs = product_type.product_attributes.all()
        attributes = ProductAttributeAssignmentMixin.clean_input(
            attributes, attributes_qs, creation=False
        )
        return attributes

    @classmethod
    def post_save_action(cls, info: ResolveInfo, instance, cleaned_input):
        product = models.Product.objects.prefetched_for_webhook(single_object=True).get(
            pk=instance.pk)
        channel_ids = set(
            product.channel_listings.all().values_list("channel_id", flat=True))
        cls.call_event(mark_active_catalogue_promotion_rules_as_dirty, channel_ids)

        manager = get_plugin_manager_promise(info.context).get()
        cls.call_event(manager.product_updated, product)

    @classmethod
    def mutate(cls, root, info: ResolveInfo, products: TList[Dict[str, Any]]):
        success = True
        errors = []

        # Initialize dictionaries and lists
        product_updates = {}
        slugs_to_update = []
        missing_slugs = []

        for product in products:
            slug = product.get('slug')
            if not slug:
                missing_slugs.append(product)
            else:
                slugs_to_update.append(slug)
                product_updates[slug] = product

        # If there are products without a slug, add an error
        if missing_slugs:
            for product in missing_slugs:
                errors.append(ProductError(field="product",
                                           message=f"Product with details {product} is missing a slug."))
            success = False

        # Query products by slug
        qs = models.Product.objects.filter(
            Q(slug__in=slugs_to_update)
        ).prefetch_related(
            "product_type__product_attributes__values",
            "product_type__attributeproduct",
        )

        # Create a mapping of found products by their slug
        found_products_by_slug = qs.values_list('slug', flat=True)
        product_slugs_set = set(found_products_by_slug)

        # Check for products that were not found
        for slug in slugs_to_update:
            if slug not in product_slugs_set:
                errors.append(ProductError(field="product",
                                           message=f"Product with slug {slug} not found."))
                success = False

        if not success:
            return ProductBulkUpdateResult(success=False, productErrors=errors)

        # Query ProductType instances
        product_type_ids = {product.get('product_type') for product in products}
        product_type_qs = models.ProductType.objects.filter(slug__in=product_type_ids)
        product_type_mapping = {product_type.id: product_type for product_type in
                                product_type_qs}

        # Query categories by slug
        category_slugs = {category_slug for product in products for category_slug in
                          product.get('categories', [])}
        category_qs = models.Category.objects.filter(slug__in=category_slugs)
        category_mapping = {category.slug: category.id for category in category_qs}

        # Update products
        for product in qs:
            product_data = product_updates.get(product.slug)
            if product_data:
                try:
                    for field, value in product_data.items():
                        if field == "product_type":

                            product_type_instance = models.ProductType.objects.filter(
                                slug=value).first()

                            if product_type_instance:
                                setattr(product, "product_type", product_type_instance)
                            else:
                                errors.append(ProductError(
                                    field="productType",
                                    message=f"ProductType with slug '{value}' not found."
                                ))
                                success = False

                        elif field == "attributes" and value:
                            product_type = product.product_type
                            value = cls.clean_attributes(value, product_type)
                            # Update attributes using ProductAttributeAssignmentMixin
                            ProductAttributeAssignmentMixin.save(product, value)
                        elif field == "category":
                            category_instance = models.Category.objects.filter(
                                slug=value).first()
                            if category_instance:
                                setattr(product, "category", category_instance)
                            else:
                                errors.append(ProductError(
                                    field="category",
                                    message=f"Category with slug '{value}' not found."
                                ))
                                success = False

                        elif field == "collections":
                            # Handle ManyToMany fields separately
                            if value is not None:
                                getattr(product, field).set(value)
                        else:
                            setattr(product, field, value)
                    product.save()
                    cls.post_save_action(info, product, product_data)
                except Exception as e:
                    success = False
                    errors.append(ProductError(field=str(product.id), message=str(e)))

        return ProductBulkUpdateResult(success=success, productErrors=errors)
