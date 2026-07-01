from django.contrib import admin

from .models import Batch, Course, LabExercise, Module


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "track", "is_active")
    list_filter = ("track", "is_active")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Module)
class ModuleAdmin(admin.ModelAdmin):
    list_display = ("course", "code", "title", "order")
    list_filter = ("course",)


@admin.register(LabExercise)
class LabExerciseAdmin(admin.ModelAdmin):
    list_display = ("title", "module", "reset_required", "submission_required", "max_marks")
    list_filter = ("reset_required", "submission_required")


@admin.register(Batch)
class BatchAdmin(admin.ModelAdmin):
    list_display = ("name", "course", "instructor", "is_active")
    list_filter = ("is_active", "course")
    filter_horizontal = ("students",)
