from django.db import transaction
from graphene import ObjectType, Boolean, Mutation, List, Field

from saleor.product import models
from saleor.attribute import models as attribute_models

from ...core.types import ProductError
from ...attribute.utils import ProductAttributeAssignmentMixin, AttrValuesInput
from ..mutations.product.product_create import ProductInput
from ..types import Product


T_INPUT_MAP = list[tuple[attribute_models.Attribute, AttrValuesInput]]


class ProductBulkCreateOrUpdateResult(ObjectType):
    success = Boolean(description="Indicates if the operation was successful.")
    errors = List(
        ProductError, description="List of errors that occurred during the operation.")
    createdProducts = List(
        Product, description="List of products created in this operation.")
    updatedProducts = List(
        Product, description="List of products updated in this operation.")


class ProductBulkCreateOrUpdate(Mutation):
    class Arguments:
        products = List(ProductInput, required=True,
                        description="List of products to create or update.")

    class Meta:
        description = "Creates or updates multiple products."
        output = ProductBulkCreateOrUpdateResult

    success = Boolean()
    errors = List(ProductError)
    createdProducts = List(Product)
    updatedProducts = List(Product)

    @classmethod
    def clean_attributes(cls, attributes: dict,
                         product_type: models.ProductType) -> dict:
        attributes_qs = product_type.product_attributes.all()
        attributes = ProductAttributeAssignmentMixin.clean_input(
            attributes, attributes_qs, creation=True
        )
        return attributes

    @classmethod
    def mutate(cls, root, info, products):
        success = True
        errors = []
        created_products = []
        updated_products = []

        with transaction.atomic():
            for product in products:
                slug = product.get('slug')
                if not slug:
                    errors.append(
                        ProductError(field="slug", message="Slug is required."))
                    success = False
                    continue

                product_data = product.copy()

                # Handle productType decoding
                product_type_slug = product_data.pop('product_type', None)
                if product_type_slug:
                    try:
                        product_type = models.ProductType.objects.get(
                            slug=product_type_slug)
                    except models.ProductType.DoesNotExist:
                        errors.append(ProductError(field="product_type",
                                                   message=f"ProductType with slug '{product_type_slug}' not found."))
                        success = False
                        continue
                else:
                    errors.append(ProductError(field="product_type",
                                               message="ProductType is required."))
                    success = False
                    continue

                # Extract many-to-many fields and the category
                category_slug = product_data.pop('category', None)
                collections = product_data.pop('collections', [])
                attributes = product_data.pop('attributes', [])

                existing_product = models.Product.objects.filter(slug=slug).first()

                if existing_product:
                    try:
                        # Update existing product fields
                        for field, value in product_data.items():
                            setattr(existing_product, field, value)

                        existing_product.product_type = product_type

                        # Update the category
                        if category_slug:
                            category_instance = models.Category.objects.filter(
                                slug=category_slug).first()
                            if category_instance:
                                existing_product.category = category_instance
                            else:
                                errors.append(ProductError(field="category",
                                                           message=f"Category with slug '{category_slug}' not found."))
                                success = False
                                continue

                        # Save existing product first
                        existing_product.save()

                        # Update many-to-many fields
                        if collections:
                            collection_ids = [
                                models.Collection.objects.get(id=coll_id).id for coll_id
                                in collections]
                            existing_product.collections.set(collection_ids)

                        # Handle attributes
                        if attributes:
                            cleaned_attributes = cls.clean_attributes(attributes,
                                                                      product_type)
                            ProductAttributeAssignmentMixin.save(existing_product,
                                                                 cleaned_attributes)

                        updated_products.append(existing_product)
                    except Exception as e:
                        success = False
                        errors.append(ProductError(field=slug, message=str(e)))
                else:
                    try:
                        # Create a new product
                        new_product = models.Product(
                            product_type=product_type,
                            slug=slug,
                        )
                        for field, value in product_data.items():
                            setattr(new_product, field, value)

                        # Set the category
                        if category_slug:
                            category_instance = models.Category.objects.filter(
                                slug=category_slug).first()
                            if category_instance:
                                new_product.category = category_instance
                            else:
                                errors.append(ProductError(field="category",
                                                           message=f"Category with slug '{category_slug}' not found."))
                                success = False
                                continue

                        new_product.save()

                        # Set many-to-many fields after saving the product
                        if collections:
                            collection_ids = [
                                models.Collection.objects.get(id=coll_id).id for coll_id
                                in collections]
                            new_product.collections.set(collection_ids)

                        # Handle attributes
                        if attributes:
                            cleaned_attributes = cls.clean_attributes(attributes,
                                                                      product_type)
                            ProductAttributeAssignmentMixin.save(new_product,
                                                                 cleaned_attributes)

                        created_products.append(new_product)
                    except Exception as e:
                        success = False
                        errors.append(ProductError(field=slug, message=str(e)))

        return ProductBulkCreateOrUpdateResult(
            success=success,
            errors=errors,
            createdProducts=created_products,
            updatedProducts=updated_products
        )
