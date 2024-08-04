from graphene import Mutation, List, Boolean, String

from ....core.exceptions import PermissionDenied
from ....permission.enums import ProductPermissions
from ....product import models
from ..types import CategoryUpdateInput
from django.db import transaction



class CategoryBulkCreateOrUpdate(Mutation):
    class Arguments:
        updates = List(CategoryUpdateInput, required=True, description="List of category updates.")

    success = Boolean()
    errors = List(String)

    @classmethod
    @transaction.atomic
    def mutate(cls, root, info, updates):
        user = info.context.user

        if not user.has_perm(ProductPermissions.MANAGE_PRODUCTS.codename):
            raise PermissionDenied("You do not have permission to perform this action.")

        errors = []
        created_or_updated_slugs = []

        for update in updates:
            try:
                category, created = models.Category.objects.update_or_create(
                    slug=update.slug,
                    defaults={
                        'name': update.name,
                        'description': update.description,
                    }
                )

                if update.parent_slug:
                    try:
                        parent = models.Category.objects.get(slug=update.parent_slug)
                        category.parent = parent
                    except models.Category.DoesNotExist:
                        errors.append(f"Parent category with slug {update.parent_slug} does not exist.")
                        category.parent = None

                category.save()
                created_or_updated_slugs.append(update.slug)

            except Exception as e:
                errors.append(f"Error processing slug {update.slug}: {str(e)}")

        return CategoryBulkCreateOrUpdate(success=len(errors) == 0, errors=errors)

    class Meta:
        description = "Bulk create or update categories."
        permissions = (ProductPermissions.MANAGE_PRODUCTS,)
